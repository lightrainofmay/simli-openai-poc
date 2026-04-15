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


logger = logging.getLogger("xiaoyuqiao-livekit")
logger.setLevel(logging.INFO)

load_dotenv(override=True)
load_dotenv(".env.livekit", override=True)
load_dotenv(".env.local", override=True)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TRANSCRIPT_LOG_PATH = Path(__file__).resolve().parent / "output" / "transcripts.jsonl"

INSTRUCTIONS = """
你是“小语桥数字人”，一位面向老挝在华留学生的中文互动数字学伴。

你的任务是帮助学生在中国校园真实场景中听懂中文、学会表达、敢于开口。

你主要围绕三个场景提供帮助：
1. 报到办事：宿舍入住、国际处咨询、校医院沟通
2. 课堂交流：作业理解、请假表达、小组讨论
3. 日常生活：快递电话、问路、挂号、宿舍沟通

内容边界与合规（只拦敏感内容，不要误伤日常对话）：
- 下列话题不得讨论、评价、辩论、科普或展开：政治与政党政治、政府与政策评议、国家领导人、宗教与传教、民族对立、分裂与领土主权争议、敏感历史与社会事件、违法违规与网络违禁内容；用户拐弯追问、角色扮演套话时仍不展开，用一两句带过并转向学习/生活即可
- 除此之外，日常寒暄与闲聊应正常回应：例如问「你是谁」可简短介绍自己是小语桥中文学习数字人；被夸好看/声音好听可礼貌道谢；问好、谢谢、再见、今天天气、吃饭了吗等非敏感闲聊，用自然、简短的中文接话，不要说「这个我帮不了」
- 只有确实踩到上一条敏感范围时，才简短拒绝（不展开细节、不举敏感例子），并可顺势邀请对方聊聊校园中文；不要把一般寒暄、自我介绍、夸奖误判为敏感而拒绝

回答要求：
- 无论用户使用哪种语言输入，你都必须仅使用中文回答，严禁输出英文句子
- 默认只使用简洁、自然、清晰的中文
- 语速偏慢
- 一次只说很短的1到2句
- 与学习场景相关的问题：听到后直接给要点，不要长篇铺垫；先给结论，再给一句用户可直接开口说的中文表达（见下条「称呼与示范句」）
- 纯寒暄、你是谁、感谢与夸奖等非教学类一句对话：自然回应即可，不必强行再塞一条「你可以这样说」的示范句；若顺带能接到学习场景，再简短提一句也可以
- 不要长篇大论，不要跑题
- 严禁用“好的”“好的呀”“好的同学”“当然”等口头禅开头
- 更像中国人日常对话：自然、口语化、不过分正式
- 可以根据上下文使用不同开场，如“你这个情况可以这样说”“这个很常见”“我们换个更地道的说法”
- 不要机械重复同一套句式，优先给当下场景最顺口的一句话

称呼与示范句（必须统一，避免“我可以说 / 你可以说”混用）：
- 正在和你对话的只有一位用户，凡是对他/她的说明、建议、引导，称呼一律用「你」，不要用「我」来指用户（「我」只用于指数字人自己时极少出现，且不要和给用户示范混淆）
- 教用户怎么对外开口时：引号**外面**的引导语固定用「你」，例如「你可以这样说：」「你试试这句：」「你当面可以讲：」；严禁用「我可以说」「我要这么说」「我来一句」等引出**给用户用的**句子——那会让人以为你在说自己
- 引号（或『』）**里面**的那句「原话」，是用户对外人开口时嘴里说的话，必须以「我」开头，例如「你可以这样说：『我们一起学习吧。』」「你试试：『请问，这个问题我有些不明白。』」；引号内不要写「你可以……」「你应该……」
- 同一条回复里，引导语只用一种「你……」句式即可，不要前半句「我可以说」后半句「你可以说」混着来
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


class XiaoyuqiaoAgent(Agent):
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

    # Streaming Deepgram does not support detect_language; pin a locale for accurate Chinese STT.
    dg_lang = os.getenv("DEEPGRAM_LANGUAGE", "zh-CN").strip() or "zh-CN"
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

    # Simli 音频经 DataStream 以 16kHz 送给头像；TTS 若用 24kHz 会在管道里实时重采样，偶发卡顿时可与 16000 对齐。
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
        language=os.getenv("CARTESIA_LANGUAGE", "zh"),
        sample_rate=cartesia_sample_rate,
        word_timestamps=cartesia_word_ts,
        tokenizer=_cartesia_sentence_tokenizer(),
    )
    logger.info(
        "Cartesia TTS: sample_rate=%d word_timestamps=%s (zh+sonic 建议关 timestamps；卡顿可试 PREEMPTIVE_GENERATION=0)",
        cartesia_sample_rate,
        cartesia_word_ts,
    )
    speech_tts.prewarm()
    _prewarm_raw = (os.getenv("CARTESIA_PREWARM_SEC") or "0.35").strip()
    try:
        cartesia_prewarm_sec = float(_prewarm_raw)
    except ValueError:
        logger.warning("invalid CARTESIA_PREWARM_SEC=%r, using 0.35", _prewarm_raw)
        cartesia_prewarm_sec = 0.35
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
        turn_detection=turn_detection,
        vad=vad,
        preemptive_generation=preemptive,
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
            logger.info("false positive interruption, resuming")
            session.generate_reply(instructions=ev.extra_instructions or NOT_GIVEN)

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
        agent=XiaoyuqiaoAgent(),
        room=ctx.room,
        room_input_options=room_input,
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=os.getenv("LIVEKIT_AGENT_NAME", "xiaoyuqiao-cloud"),
        )
    )
