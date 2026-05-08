# 英语口语语伴 · LiveKit 实时数字人

独立仓库版本：底层链路与原「小语桥」一致，角色与产品形态改为 **中国学生英语口语陪练**。  
技术栈：**LiveKit Agents + Deepgram STT + Cartesia TTS + OpenAI/OpenRouter + Simli Avatar**。

## 快速启动

```bash
cd english_speaking_buddy   # 本仓库根目录
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.livekit.example .env.livekit
# 编辑 .env.livekit：填入 LiveKit / Simli / Deepgram / Cartesia / LLM 密钥
python3 livekit_simli_agent.py download-files
./start_all.sh
```

- 页面：<http://127.0.0.1:8030>
- 日志：`.logs/agent.log`、`.logs/realtime.log`
- 停止：`./stop_all.sh`

`start_all.sh` 默认使用 **本目录下的 `.venv`**；若要用全局虚拟环境，可设置 `VENV_PATH`。

## 与「小语桥」的差异（产品层）

- Agent：英语口语语伴（鼓励开口、轻量纠错、短句地道表达）
- STT：`en-US`（流式识别需固定语言）
- TTS：英文 Cartesia
- Web：校园 / 面试 / 出行等场景卡片
- `/api/translate`：英文气泡下提供**简体中文释义**

## LiveKit Cloud 部署

- Agent 镜像：`Dockerfile`（含 `download-files`）
- 仅 Web + Token API：`Dockerfile.web` + `railway.toml`
- `livekit.toml` 由 CLI 生成且已 gitignore；可参考 `livekit.toml.example` 自行创建

## 目录说明

| 文件 | 说明 |
|------|------|
| `livekit_simli_agent.py` | LiveKit Worker：STT/LLM/TTS/Simli |
| `realtime_server.py` | FastAPI：页面、token、翻译 API |
| `realtime_web/index.html` | 浏览器客户端 |
| `start_all.sh` / `stop_all.sh` | 本地一键启停 |

## Git 远程（可选）

本目录已是独立 Git 仓库时，可关联你自己的远端：

```bash
git remote add origin <你的仓库 URL>
git push -u origin main
```

若当前仍放在 `simli_openai_poc` 工作区内，也可整体 `mv` 到任意路径后再 `git init` / 推送。
