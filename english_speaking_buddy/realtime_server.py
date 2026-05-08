from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, RoomAgentDispatch, RoomConfiguration, VideoGrants
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "realtime_web"
logger = logging.getLogger("english-buddy-web")

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.livekit")

# 页面场景卡片：英语口语练习常见场景
DEMO_SCENARIOS: list[dict[str, str]] = [
    {
        "id": "campus",
        "titleEn": "Campus Life",
        "titleZh": "校园交流",
        "blurbEn": "Dorm check-in · ask teacher · group discussion",
        "blurbZh": "宿舍入住 · 课堂提问 · 小组讨论",
        "prompt": "I want to ask my teacher for an extension politely.",
    },
    {
        "id": "interview",
        "titleEn": "Interview",
        "titleZh": "求职面试",
        "blurbEn": "Self-introduction · strengths · project storytelling",
        "blurbZh": "自我介绍 · 优势表达 · 项目描述",
        "prompt": "Please help me answer: What are your strengths?",
    },
    {
        "id": "travel",
        "titleEn": "Travel & Daily",
        "titleZh": "出行与日常",
        "blurbEn": "Ask directions · restaurant order · airport check-in",
        "blurbZh": "问路打车 · 餐厅点餐 · 机场值机",
        "prompt": "How do I ask for directions to the train station?",
    },
]

app = FastAPI(title="English Speaking Buddy Realtime Web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class TokenRequest(BaseModel):
    room: str
    identity: str
    name: str | None = None


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_TRANSLATE_SYSTEM = (
    "You translate into Simplified Chinese. Output ONLY the translation text: "
    "no quotes, no labels, no explanation. "
    "If the input is already entirely in Chinese, output it unchanged (only trim spaces). "
    "Preserve names in their original script when natural. "
    "Prefer education and spoken-English wording when multiple translations are possible."
)


_REPEATED_CHUNK_RE = re.compile(r"(.{2,4})\1{6,}")


def _sanitize_zh_translation(text: str) -> str:
    """Drop obviously corrupted/hallucinated translation outputs."""
    zh = (text or "").strip()
    if not zh:
        return ""

    # Remove wrapping quotes once more defensively.
    if zh.startswith('"') and zh.endswith('"') and len(zh) >= 2:
        zh = zh[1:-1].strip()
    if zh.startswith("「") and zh.endswith("」") and len(zh) >= 2:
        zh = zh[1:-1].strip()

    compact = re.sub(r"\s+", "", zh)
    if not compact:
        return ""

    # Hard cap to avoid runaway generations.
    if len(zh) > 320:
        zh = zh[:320].rstrip()
        compact = re.sub(r"\s+", "", zh)

    # Typical corruption pattern in screenshots: short chunk repeated many times.
    if _REPEATED_CHUNK_RE.search(compact):
        return ""

    # Another signal: very long text with extremely low character diversity.
    if len(compact) >= 60:
        diversity = len(set(compact)) / max(1, len(compact))
        if diversity < 0.16:
            return ""

    return zh


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing {name}")
    return value


async def _create_dispatch_fallback(*, room: str, agent_name: str) -> tuple[bool, str]:
    """Best-effort dispatch fallback for cases where room_config dispatch is missed."""
    try:
        from livekit import api as lkapi  # import lazily to keep module import lightweight

        client = lkapi.LiveKitAPI(
            url=require_env("LIVEKIT_URL"),
            api_key=require_env("LIVEKIT_API_KEY"),
            api_secret=require_env("LIVEKIT_API_SECRET"),
        )
        try:
            resp = await client.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    room=room,
                    agent_name=agent_name,
                    metadata='{"source":"realtime_web_fallback"}',
                )
            )
            msg = f"dispatch fallback created: room={room} agent={agent_name} id={getattr(resp, 'id', '')}"
            logger.info(msg)
            return True, msg
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        # Do not block token issuance if fallback dispatch fails.
        msg = f"dispatch fallback failed: room={room} agent={agent_name} err={exc}"
        logger.warning(msg)
        return False, msg


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        WEB_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/config")
def config() -> JSONResponse:
    return JSONResponse(
        {
            "livekitUrl": require_env("LIVEKIT_URL"),
            "defaultRoom": os.getenv("LIVEKIT_DEFAULT_ROOM", "english-speaking-room"),
            "defaultIdentity": os.getenv("LIVEKIT_DEFAULT_IDENTITY", "student-demo"),
            "agentName": os.getenv("LIVEKIT_AGENT_NAME", "english-speaking-buddy-cloud"),
            "showAdvancedConnection": _env_flag("WEB_SHOW_ADVANCED", "1"),
            "scenarios": DEMO_SCENARIOS,
        },
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/token")
def create_token(req: TokenRequest) -> JSONResponse:
    api_key = require_env("LIVEKIT_API_KEY")
    api_secret = require_env("LIVEKIT_API_SECRET")
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "english-speaking-buddy-cloud")

    room = req.room.strip()
    identity = req.identity.strip()
    if not room or not identity:
        raise HTTPException(status_code=400, detail="room and identity are required")

    display_name = (req.name or identity).strip()
    # Force unique participant identity to avoid "same identity" collisions
    # across multiple browser tabs/reloads.
    unique_identity = f"{identity}-{uuid4().hex[:6]}"

    token = (
        AccessToken(api_key, api_secret)
        .with_identity(unique_identity)
        .with_name(display_name)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            RoomConfiguration(
                agents=[
                    RoomAgentDispatch(
                        agent_name=agent_name,
                        metadata='{"source":"realtime_web"}',
                    )
                ]
            )
        )
        .to_jwt()
    )

    # Fallback dispatch: create an explicit dispatch in addition to room_config.
    # This improves reliability when automatic dispatch is delayed or missed.
    dispatch_ok = False
    dispatch_msg = ""
    try:
        dispatch_ok, dispatch_msg = asyncio.run(
            _create_dispatch_fallback(room=room, agent_name=agent_name)
        )
    except Exception as exc:  # noqa: BLE001
        dispatch_msg = f"dispatch fallback run failed: {exc}"
        logger.warning(dispatch_msg)

    return JSONResponse(
        {
            "token": token,
            "identity": unique_identity,
            "dispatchFallbackOk": dispatch_ok,
            "dispatchFallbackMsg": dispatch_msg,
        }
    )


@app.post("/api/translate")
def translate_to_zh(req: TranslateRequest) -> JSONResponse:
    """网页对话区：给英文句子补充中文释义（与 Agent 共用 OPENROUTER_API_KEY）。"""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return JSONResponse({"translation": "", "ok": False})

    model = (
        os.getenv("OPENROUTER_TRANSLATE_MODEL", "").strip()
        or os.getenv("OPENROUTER_LLM_MODEL", "").strip()
        or "openai/gpt-4o-mini"
    )
    text = req.text.strip()
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _TRANSLATE_SYSTEM},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 220,
                    "temperature": 0.0,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        raw_translation = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()
        translation = _sanitize_zh_translation(raw_translation)
        if not translation and raw_translation:
            logger.warning("translation output dropped as corrupted: %.160s", raw_translation)
        return JSONResponse({"translation": translation, "ok": True})
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response else str(exc)
        raise HTTPException(status_code=502, detail=f"OpenRouter error: {detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

