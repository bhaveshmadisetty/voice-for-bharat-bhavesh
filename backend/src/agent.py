import asyncio
import logging

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    UserStateChangedEvent,
    cli,
    inference,
    tokenize,
    room_io,
    UserInputTranscribedEvent,
)
from livekit.plugins import murf, silero, google, deepgram, noise_cancellation
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("agent")

load_dotenv(".env.local")

# The agent's instructions live in prompts.py so they can be iterated
# without touching the pipeline code below.
from prompts import (
    GREETING_INSTRUCTIONS,
    SILENCE_CLOSING_INSTRUCTIONS,
    SILENCE_REPROMPT_INSTRUCTIONS,
    SYSTEM_PROMPT,
)


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)

    async def on_enter(self) -> None:
        """Speak first, so the customer knows who picked up and what we do.

        Generated rather than a fixed string, so the wording varies between
        calls and the agent is free to open in whichever language fits.
        """
        self.session.generate_reply(instructions=GREETING_INSTRUCTIONS)

    # To add tools, use the @function_tool decorator.
    # Here's an example that adds a simple weather tool.
    # You also have to add `from livekit.agents import function_tool, RunContext` to the top of this file
    # @function_tool
    # async def lookup_weather(self, context: RunContext, location: str):
    #     """Use this tool to look up current weather information in the given location.
    #
    #     If the location is not supported by the weather service, the tool will indicate this. You must tell the user the location's weather is unavailable.
    #
    #     Args:
    #         location: The location to look up weather information for (e.g. city name)
    #     """
    #
    #     logger.info(f"Looking up weather for {location}")
    #
    #     return "sunny with a temperature of 70 degrees."


# Loading Silero VAD plus the multilingual turn detector takes ~10-15s on a cold
# start, which overruns the 10s default and kills the worker before it registers.
server = AgentServer(initialize_process_timeout=60.0)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Set up a voice AI pipeline using Murf Falcon, Gemini, Deepgram, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        # "multi" turns on code-switching, so a customer can say
        # "do kilo sugar aur ek Amul butter" in one breath and have both the
        # Hindi and the English land in the same transcript. The default
        # (en-US) mangles anything not in English.
        stt=deepgram.STT(model="nova-3", language="multi"),
        # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
        # See all available models at https://docs.livekit.io/agents/models/llm/
        llm=google.LLM(
                model="gemini-3.5-flash-lite",
            ),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        tts=murf.TTS(
                voice="Anisha",
                # The locale is fixed for the whole session and no Indian voice
                # supports both en-IN and hi-IN, so it cannot follow the
                # customer's language switch. en-IN is the better compromise:
                # it reads English cleanly and still handles romanised Hindi
                # ("do kilo", "theek hai") acceptably. hi-IN does the reverse,
                # giving plain English a heavy Hindi accent.
                locale="en-IN",
                style="Conversation",
                tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=2),
                text_pacing=True
            ),
        # VAD and turn detection are used to determine when the user is speaking and when the agent should respond
        # See more at https://docs.livekit.io/agents/build/turns
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
        # How long the customer can stay quiet before we treat them as away.
        # Kirana calls have real pauses — someone checks the kitchen shelf
        # mid-order — so this is longer than the 15s default.
        user_away_timeout=20.0,
    )

    # Handle the customer going quiet: nudge once, then close out politely
    # rather than holding a dead line open.
    silence_strikes = 0

    @session.on("user_state_changed")
    def on_user_state_changed(ev: UserStateChangedEvent) -> None:
        nonlocal silence_strikes

        if ev.new_state != "away":
            # They came back — any earlier silence no longer counts against them.
            if ev.new_state == "speaking":
                silence_strikes = 0
            return

        silence_strikes += 1
        logger.info("user went quiet (strike %d)", silence_strikes)

        if silence_strikes == 1:
            session.generate_reply(instructions=SILENCE_REPROMPT_INSTRUCTIONS)
        else:
            # Second failure: say goodbye, then end the session once the
            # closing line has actually finished playing.
            async def close_after_goodbye() -> None:
                handle = session.generate_reply(
                    instructions=SILENCE_CLOSING_INSTRUCTIONS
                )
                await handle.wait_for_playout()
                await session.aclose()

            asyncio.create_task(close_after_goodbye())

    # To use a realtime model instead of a voice pipeline, use the following session setup instead.
    # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/))
    # 1. Install livekit-agents[openai]
    # 2. Set OPENAI_API_KEY in .env.local
    # 3. Add `from livekit.plugins import openai` to the top of this file
    # 4. Use the following session setup instead of the version above
    # session = AgentSession(
    #     llm=openai.realtime.RealtimeModel(voice="marin")
    # )

    # # Add a virtual avatar to the session, if desired
    # # For other providers, see https://docs.livekit.io/agents/models/avatar/
    # avatar = hedra.AvatarSession(
    #   avatar_id="...",  # See https://docs.livekit.io/agents/models/avatar/plugins/hedra
    # )
    # # Start the avatar and wait for it to join
    # await avatar.start(session, room=ctx.room)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: (
                    noise_cancellation.BVCTelephony()
                    if params.participant.kind
                    == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                    else noise_cancellation.BVC()
                ),
            ),
        ),
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
