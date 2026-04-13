from __future__ import annotations

import os
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

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.livekit")

# 正式页「实用场景」：老挝语标题 + 中文说明 + 一键填入输入框的示例问法
DEMO_SCENARIOS: list[dict[str, str]] = [
    {
        "id": "checkin",
        "titleLao": "ເຊັກອີນ",
        "titleZh": "报到办事",
        "blurbLao": "ການເຂົ້າພັກທີ່ຫໍ · ສຳນັກງານສາກົນ · ການຕິດຕໍ່ສື່ສານກັບໂຮງໝໍໂຮງຮຽນ",
        "blurbZh": "宿舍入住 · 国际处 · 校医院沟通",
        "prompt": "我刚到学校要去宿管处办入住，用中文该怎么开口？",
    },
    {
        "id": "classroom",
        "titleLao": "ໃນຫ້ອງຮຽນ",
        "titleZh": "课堂交流",
        "blurbLao": "ການເຂົ້າໃຈວຽກບ້ານ · ການສະແດງອອກໃນການລາພັກ · ກຸ່ມສົນທະນາ",
        "blurbZh": "作业理解 · 请假表达 · 小组讨论",
        "prompt": "老师布置的作业我没完全听懂，用中文怎么礼貌地问？",
    },
    {
        "id": "daily",
        "titleLao": "ຊີວິດປະຈຳວັນ",
        "titleZh": "日常生活",
        "blurbLao": "ເບີໂທພະນັກງານຂົ່ນສົ່ງ · ຖາມທາງ · ການລົງທະບຽນ ແລະ ການສື່ສານກ່ຽວກັບຫໍພັກ",
        "blurbZh": "快递电话 · 问路 · 挂号与宿舍沟通",
        "prompt": "我想打电话问快递到哪了，中文电话开头怎么说？",
    },
]

app = FastAPI(title="Xiaoyuqiao Realtime Web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class TokenRequest(BaseModel):
    room: str
    identity: str
    name: str | None = None


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_TRANSLATE_SYSTEM = (
    "You translate into Lao as used in Laos. Output ONLY the translation text: "
    "no quotes, no labels, no explanation. "
    "If the input is already entirely in Lao, output it unchanged (only trim spaces). "
    "Preserve names in their original script when natural. "
    "Prefer the following fixed glossary when these Chinese phrases appear: "
    "中文之桥=ຂົວພາສາຈີນ; "
    "老挝留学生在华校园中文学伴=ຄູ່ສອນພາສາຈີນສຳລັບນັກສຶກສາລາວໃນວິທະຍາເຂດຈີນ; "
    "三个实用场景·点击卡片可将示例问题填入输入框="
    "ສາມສະຖານະການທີ່ໃຊ້ໄດ້ຈິງ: ການຄລິກທີ່ບັດຊ່ວຍໃຫ້ທ່ານສາມາດຕື່ມຂໍ້ມູນຕົວຢ່າງຄຳຖາມໃນກ່ອງປ້ອນຂໍ້ມູນໄດ້; "
    "报到办事=ເຊັກອີນ; "
    "快递电话=ເບີໂທພະນັກງານຂົ່ນສົ່ງ; "
    "国际处=ສຳນັກງານສາກົນ; "
    "校医院沟通=ການຕິດຕໍ່ສື່ສານກັບໂຮງໝໍໂຮງຮຽນ; "
    "作业理解=ການເຂົ້າໃຈວຽກບ້ານ; "
    "请假表达=ການສະແດງອອກໃນການລາພັກ; "
    "挂号与宿舍沟通=ການລົງທະບຽນ ແລະ ການສື່ສານກ່ຽວກັບຫໍພັກ."
)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"Missing {name}")
    return value


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/config")
def config() -> JSONResponse:
    return JSONResponse(
        {
            "livekitUrl": require_env("LIVEKIT_URL"),
            "defaultRoom": os.getenv("LIVEKIT_DEFAULT_ROOM", "xiaoyuqiao-room"),
            "defaultIdentity": os.getenv("LIVEKIT_DEFAULT_IDENTITY", "student-demo"),
            "agentName": os.getenv("LIVEKIT_AGENT_NAME", "xiaoyuqiao"),
            "showAdvancedConnection": _env_flag("WEB_SHOW_ADVANCED", "1"),
            "scenarios": DEMO_SCENARIOS,
        }
    )


@app.post("/api/token")
def create_token(req: TokenRequest) -> JSONResponse:
    api_key = require_env("LIVEKIT_API_KEY")
    api_secret = require_env("LIVEKIT_API_SECRET")
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "xiaoyuqiao")

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

    return JSONResponse({"token": token, "identity": unique_identity})


@app.post("/api/translate")
def translate_to_lao(req: TranslateRequest) -> JSONResponse:
    """网页对话区：在拼音/汉字下展示老挝语字幕（与 Agent 共用 OPENROUTER_API_KEY）。"""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        return JSONResponse({"lao": "", "ok": False})

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
                    "max_tokens": 1024,
                    "temperature": 0.2,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        lao = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()
        if lao.startswith('"') and lao.endswith('"') and len(lao) >= 2:
            lao = lao[1:-1].strip()
        if lao.startswith("「") and lao.endswith("」") and len(lao) >= 2:
            lao = lao[1:-1].strip()
        return JSONResponse({"lao": lao, "ok": True})
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response else str(exc)
        raise HTTPException(status_code=502, detail=f"OpenRouter error: {detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

