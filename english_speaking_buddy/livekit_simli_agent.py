from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from livekit.agents import (
    NOT_GIVEN,
    Agent,
    AgentFalseInterruptionEvent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    RoomInputOptions,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    metrics,
    tokenize,
)
from livekit.agents.voice.room_io.types import TextInputEvent
from livekit.plugins import cartesia, deepgram, openai, silero, simli
from livekit.plugins.turn_detector.multilingual import MultilingualModel

try:
    from livekit.plugins import noise_cancellation
except ImportError:
    noise_cancellation = None  # type: ignore[assignment]


logger = logging.getLogger("english-buddy-livekit")
logger.setLevel(logging.INFO)

load_dotenv(override=True)
load_dotenv(".env.livekit", override=True)
load_dotenv(".env.local", override=True)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TRANSCRIPT_LOG_PATH = Path(__file__).resolve().parent / "output" / "transcripts.jsonl"

INSTRUCTIONS = """
你是“英语口语语伴”数字人，服务对象是中国学生。

你的核心任务：
1. 陪练真实可用的英语口语（校园、面试、旅行、日常、课堂表达）
2. 先鼓励学生开口，再做轻量纠错，不打击积极性
3. 给出可以直接复述的一句地道英语

回复风格：
- 默认优先用英语回复，语速慢，句子短，一次 1-3 句
- 如果学生明显没听懂，可补 1 句简短中文解释
- 少讲语法术语，多给“直接能说”的表达
- 当学生用中文提问时：先给英文说法，再给一句中文提示
- 避免长篇大论，避免一次给太多替代表达

纠错规则：
- 优先纠正影响沟通的错误（时态、人称、常见搭配、礼貌表达）
- 纠错格式尽量简洁：
  Your sentence: ...
  Better: ...
- 每次最多纠 1-2 个点，然后让学生再说一遍

安全边界：
- 不讨论敏感政治、违法违规、仇恨、色情、暴力等内容
- 如果用户触及以上话题，简短拒绝并拉回英语学习场景
""".strip()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


class _CartesiaTokenizerShim:
    """Delegate to blingfire without subclassing ``tokenize.SentenceTokenizer``.

    The Cartesia plugin sets ``max_buffer_delay_ms=0`` when it sees a SentenceTokenizer, which
    makes the first second of streaming audio very choppy; omitting that flag restores server-side
    buffering defaults.
    """

    def __init__(self, inner: tokenize.blingfire.SentenceTokenizer) -> None:
        self._inner = inner

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return self._inner.tokenize(text, language=language)

    def stream(self, *, language: str | None = None) -> tokenize.SentenceStream:
        return self._inner.stream(language=language)


def _cartesia_sentence_tokenizer() -> _CartesiaTokenizerShim:
    raw = (os.getenv("CARTESIA_MIN_SENTENCE_LEN") or "20").strip()
    try:
        min_len = max(1, int(raw, 10))
    except ValueError:
        logger.warning("invalid CARTESIA_MIN_SENTENCE_LEN=%r, using 20", raw)
        min_len = 20
    return _CartesiaTokenizerShim(tokenize.blingfire.SentenceTokenizer(min_sentence_len=min_len))


def should_ignore_tts_only_transcript(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True

    normalized = "".join(ch for ch in cleaned if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    canned_phrases = (
        "好的，请提供您希望转写的语音。",
        "好的请提供您希望转写的语音。",
        "好的请提供语音内容我将帮助您进行转写。",
        "好的请提供语音内容我将帮您进行转写。",
        "好的请提供语音内容我会帮您进行转写。",
        "请提供您希望转写的语音。",
        "请提供你希望转写的语音。",
        "请提供您要转写的语音。",
    )
    normalized_canned = {
        "".join(ch for ch in p if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
        for p in canned_phrases
    }
    if normalized in normalized_canned:
        return True

    has_transcribe_intent = ("转写" in normalized) or ("语音" in normalized and "提供" in normalized)
    if has_transcribe_intent and ("请" in normalized or "好的" in normalized):
        return True

    short_noise_like = {"嗯", "啊", "喂"}
    if normalized in short_noise_like:
        return True

    return False


class EnglishSpeakingBuddyAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


def load_silero_vad() -> silero.VAD:
    return silero.VAD.load(
        min_speech_duration=float(os.getenv("VAD_MIN_SPEECH_SEC", "0.10")),
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE_SEC", "0.35")),
        prefix_padding_duration=0.30,
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.50")),
        sample_rate=16000,
    )


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using %s", name, raw, default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def build_turn_handling(
    *,
    turn_detection: object,
    preemptive_generation_enabled: bool,
) -> TurnHandlingOptions:
    """Tune endpointing vs latency and how easily user audio can interrupt agent TTS."""
    min_ep = _float_env("ENDPOINTING_MIN_DELAY_SEC", 0.42)
    max_ep = _float_env("ENDPOINTING_MAX_DELAY_SEC", 3.0)
    min_intr = _float_env("INTERRUPTION_MIN_DURATION_SEC", 0.85)
    false_raw = (os.getenv("FALSE_INTERRUPTION_TIMEOUT_SEC") or "2.5").strip().lower()
    if false_raw in ("none", "off", "disable", "-1"):
        false_timeout: float | None = None
    else:
        try:
            false_timeout = float(false_raw)
        except ValueError:
            logger.warning("invalid FALSE_INTERRUPTION_TIMEOUT_SEC=%r, using 2.5", false_raw)
            false_timeout = 2.5

    interruption: dict = {
        "min_duration": min_intr,
        "resume_false_interruption": _bool_env("RESUME_FALSE_INTERRUPPTION", True),
    }
    if false_timeout is not None:
        interruption["false_interruption_timeout"] = false_timeout
    if not _bool_env("INTERRUPTION_ENABLED", True):
        interruption["enabled"] = False

    th: TurnHandlingOptions = {
        "turn_detection": turn_detection,  # type: ignore[typeddict-item]
        "endpointing": {"min_delay": min_ep, "max_delay": max_ep},
        "interruption": interruption,  # type: ignore[typeddict-item]
        "preemptive_generation": {"enabled": preemptive_generation_enabled},
    }
    logger.info(
        "turn_handling: endpointing min/max=%.2f/%.2fs, interrupt min_duration=%.2fs, "
        "preemptive_gen=%s",
        min_ep,
        max_ep,
        min_intr,
        preemptive_generation_enabled,
    )
    return th


def prewarm(proc: JobProcess) -> None:
    # Warmed worker processes may expose VAD here; some job subprocess paths omit it.
    proc.userdata["vad"] = load_silero_vad()


async def entrypoint(ctx: JobContext) -> None:
    def append_transcript_log(record: dict) -> None:
        TRANSCRIPT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRANSCRIPT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def publish_user_transcript(text: str) -> None:
        payload = json.dumps({"type": "user_transcript", "text": text}, ensure_ascii=False)
        try:
            await ctx.room.local_participant.publish_data(
                payload.encode("utf-8"),
                reliable=True,
                topic="agent.user",
            )
            logger.info("published user_transcript to data channel: %s", text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to publish user_transcript: %s", exc)

    async def publish_spoken_text(text: str) -> None:
        payload = json.dumps({"type": "tts_spoken_text", "text": text}, ensure_ascii=False)
        try:
            await ctx.room.local_participant.publish_data(
                payload.encode("utf-8"),
                reliable=True,
                topic="agent.tts",
            )
            logger.info("published spoken_text to data channel")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to publish spoken_text: %s", exc)

    async def publish_llm_text(text: str) -> None:
        payload = json.dumps({"type": "llm_text", "text": text}, ensure_ascii=False)
        try:
            await ctx.room.local_participant.publish_data(
                payload.encode("utf-8"),
                reliable=True,
                topic="agent.llm",
            )
            logger.info("published llm_text to data channel")
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to publish llm_text: %s", exc)

    ctx.log_context_fields = {"room": ctx.room.name}

    logger.info("connecting to LiveKit room: %s", ctx.room.name)
    await ctx.connect()

    use_tts_only = os.getenv("USE_TTS_ONLY", "0").lower() not in ("0", "false", "no")
    use_openrouter_llm = os.getenv("USE_OPENROUTER_LLM", "0").lower() not in ("0", "false", "no")
    preemptive = os.getenv("PREEMPTIVE_GENERATION", "1").lower() not in ("0", "false", "no")
    disable_bvc = os.getenv("DISABLE_BVC", "0").lower() not in ("0", "false", "no")

    require_env("DEEPGRAM_API_KEY")
    require_env("CARTESIA_API_KEY")
    require_env("SIMLI_API_KEY")
    require_env("SIMLI_FACE_ID")

    openrouter_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    llm_model = os.getenv("OPENROUTER_LLM_MODEL", "openai/gpt-4o-mini")

    if not use_tts_only:
        if use_openrouter_llm:
            if not openrouter_key:
                raise RuntimeError("Missing or blank env var: OPENROUTER_API_KEY")
        elif not openai_key:
            raise RuntimeError("Missing or blank env var: OPENAI_API_KEY")
    elif not openrouter_key:
        raise RuntimeError("Missing or blank env var: OPENROUTER_API_KEY")

    # Streaming Deepgram does not support detect_language; pin a locale for accurate English STT.
    dg_lang = os.getenv("DEEPGRAM_LANGUAGE", "en-US").strip() or "en-US"
    stt = deepgram.STT(
        model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
        language=dg_lang,
    )

    turn_raw = os.getenv("TURN_DETECTION", "stt").strip().lower()
    if turn_raw in ("multilingual", "ml", "eou"):
        turn_detection = MultilingualModel()
    elif turn_raw == "vad":
        turn_detection = "vad"
    elif turn_raw == "stt":
        turn_detection = "stt"
    else:
        logger.warning("unknown TURN_DETECTION=%r, using stt", turn_raw)
        turn_detection = "stt"

    # Simli audio is sent via DataStream at 16kHz; align TTS to reduce resampling overhead.
    _csr = (os.getenv("CARTESIA_SAMPLE_RATE") or "16000").strip()
    try:
        cartesia_sample_rate = int(_csr, 10)
    except ValueError:
        logger.warning("invalid CARTESIA_SAMPLE_RATE=%r, using 16000", _csr)
        cartesia_sample_rate = 16000
    cartesia_word_ts = os.getenv("CARTESIA_WORD_TIMESTAMPS", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    speech_tts = cartesia.TTS(
        voice=os.getenv("CARTESIA_VOICE_ID", "6eb8965c-e295-47bd-a9e4-3eeebb3abcff"),
        model=os.getenv("CARTESIA_MODEL", "sonic-3"),
        language=os.getenv("CARTESIA_LANGUAGE", "en"),
        sample_rate=cartesia_sample_rate,
        word_timestamps=cartesia_word_ts,
        tokenizer=_cartesia_sentence_tokenizer(),
    )
    logger.info(
        "Cartesia TTS: sample_rate=%d word_timestamps=%s (for sonic voices, keep timestamps off if unstable)",
        cartesia_sample_rate,
        cartesia_word_ts,
    )
    speech_tts.prewarm()
    _prewarm_raw = (os.getenv("CARTESIA_PREWARM_SEC") or "0.12").strip()
    try:
        cartesia_prewarm_sec = float(_prewarm_raw)
    except ValueError:
        logger.warning("invalid CARTESIA_PREWARM_SEC=%r, using 0.12", _prewarm_raw)
        cartesia_prewarm_sec = 0.12
    if cartesia_prewarm_sec > 0:
        logger.info(
            "Cartesia prewarm: sleep %.2fs so first TTS websocket is ready (set CARTESIA_PREWARM_SEC=0 to skip)",
            cartesia_prewarm_sec,
        )
        await asyncio.sleep(cartesia_prewarm_sec)

    if use_tts_only:
        llm_component = NOT_GIVEN
    elif use_openrouter_llm:
        llm_component = openai.LLM(
            model=llm_model,
            base_url=OPENROUTER_BASE_URL,
            api_key=openrouter_key,
        )
    else:
        openai_model = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")
        llm_component = openai.LLM(model=openai_model, api_key=openai_key)

    if use_tts_only:
        logger.info("LLM path: USE_TTS_ONLY=1 (httpx → OpenRouter) model=%s", llm_model)
    elif use_openrouter_llm:
        logger.info(
            "LLM path: OpenRouter via LiveKit plugin base_url=%s model=%s",
            OPENROUTER_BASE_URL,
            llm_model,
        )
    else:
        logger.info("LLM path: OpenAI official API model=%s", openai_model)

    vad = ctx.proc.userdata.get("vad")
    if vad is None:
        logger.warning("VAD not in prewarm userdata; loading Silero in this job process")
        vad = load_silero_vad()

    session = AgentSession(
        stt=stt,
        llm=llm_component,
        tts=speech_tts,
        vad=vad,
        turn_handling=build_turn_handling(
            turn_detection=turn_detection,
            preemptive_generation_enabled=preemptive,
        ),
    )

    async def tts_only_reply(transcript: str) -> None:
        """OpenRouter + Cartesia for USE_TTS_ONLY=1 (STT or text input)."""
        t = (transcript or "").strip()
        if not t:
            return
        if should_ignore_tts_only_transcript(t):
            logger.info("ignore likely-noise transcript in tts-only mode: %s", t)
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            await publish_user_transcript(t)
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": llm_model,
                        "messages": [
                            {"role": "system", "content": INSTRUCTIONS},
                            {"role": "user", "content": t},
                        ],
                        "max_tokens": 200,
                    },
                )
            resp.raise_for_status()
            reply_text = (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            ).strip()
            if not reply_text:
                logger.warning("LLM returned empty reply for: %s", t)
                return
            logger.info("llm reply(tts-only mode): %s", reply_text)
            await publish_llm_text(reply_text)
            await publish_spoken_text(reply_text)
            await asyncio.to_thread(
                append_transcript_log,
                {
                    "timestamp": now,
                    "room": ctx.room.name,
                    "mode": "tts_only",
                    "user_transcript": t,
                    "spoken_text": reply_text,
                },
            )
            await session.say(
                reply_text,
                allow_interruptions=False,
                add_to_chat_ctx=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("tts-only llm reply failed: %s", exc)

    async def on_room_text_input(sess: AgentSession, ev: TextInputEvent) -> None:
        text = (ev.text or "").strip()
        if not text:
            return
        logger.info("user text input (lk.chat): %s", text)
        await sess.interrupt()
        if use_tts_only:
            asyncio.create_task(tts_only_reply(text))
        else:
            await publish_user_transcript(text)
            sess.generate_reply(user_input=text)

    if not use_tts_only:

        @session.on("agent_false_interruption")
        def _on_agent_false_interruption(ev: AgentFalseInterruptionEvent) -> None:
            # Runtime already resumes TTS when a false interrupt is detected; do not call
            # generate_reply here — that schedules a new turn and can truncate playback.
            if ev.resumed:
                logger.info("agent_false_interruption: playback resumed by runtime")
            else:
                logger.warning(
                    "agent_false_interruption: runtime did not resume (audio may not support pause); "
                    "check FALSE_INTERRUPTION_TIMEOUT_SEC / RESUME_FALSE_INTERRUPTION"
                )

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage() -> None:
        summary = usage_collector.get_summary()
        logger.info("Usage: %s", summary)

    ctx.add_shutdown_callback(log_usage)

    if not use_tts_only:

        @session.on("user_input_transcribed")
        def _on_user_input_transcribed(ev) -> None:
            transcript = (getattr(ev, "transcript", "") or "").strip()
            is_final = bool(getattr(ev, "is_final", False))
            if not is_final or not transcript:
                return
            logger.info("user_input_transcribed(final): %s", transcript)
            asyncio.create_task(publish_user_transcript(transcript))

        @session.on("agent_speech_committed")
        def _on_agent_speech_committed(ev) -> None:
            reply_text = ""
            speech = getattr(ev, "speech", None)
            if speech is not None:
                reply_text = (getattr(speech, "text", "") or "").strip()
            if not reply_text:
                msg = getattr(ev, "msg", None)
                if msg is not None:
                    reply_text = (getattr(msg, "text_content", "") or "").strip()
            if not reply_text:
                return
            logger.info("agent_speech_committed reply: %s", reply_text)
            asyncio.create_task(publish_llm_text(reply_text))
            asyncio.create_task(publish_spoken_text(reply_text))
            now = datetime.now(timezone.utc).isoformat()
            asyncio.create_task(
                asyncio.to_thread(
                    append_transcript_log,
                    {
                        "timestamp": now,
                        "room": ctx.room.name,
                        "mode": "starter",
                        "spoken_text": reply_text,
                    },
                )
            )

    else:
        logger.info("USE_TTS_ONLY enabled: OpenRouter LLM + Cartesia TTS (no session LLM)")

        @session.on("user_input_transcribed")
        def _on_user_input_transcribed(ev) -> None:
            transcript = (getattr(ev, "transcript", "") or "").strip()
            is_final = bool(getattr(ev, "is_final", False))
            if not is_final or not transcript:
                return
            asyncio.create_task(tts_only_reply(transcript))

    _simli_emotion = (os.getenv("SIMLI_EMOTION_ID") or "92f24a0c-f046-45df-8df0-af7449c04571").strip()
    avatar = simli.AvatarSession(
        simli_config=simli.SimliConfig(
            api_key=require_env("SIMLI_API_KEY"),
            face_id=require_env("SIMLI_FACE_ID"),
            emotion_id=_simli_emotion,
            max_session_length=1800,
            max_idle_time=300,
        ),
    )
    logger.info("Simli avatar emotion_id=%s", _simli_emotion)

    nc = None
    if not disable_bvc and noise_cancellation is not None:
        try:
            nc = noise_cancellation.BVC()
        except Exception as exc:  # noqa: BLE001
            logger.warning("noise cancellation BVC unavailable: %s", exc)

    room_input = (
        RoomInputOptions(noise_cancellation=nc, text_input_cb=on_room_text_input)
        if nc
        else RoomInputOptions(text_input_cb=on_room_text_input)
    )

    await avatar.start(session, room=ctx.room)
    await session.start(
        agent=EnglishSpeakingBuddyAgent(),
        room=ctx.room,
        room_input_options=room_input,
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=os.getenv("LIVEKIT_AGENT_NAME", "english-speaking-buddy-cloud"),
        )
    )
