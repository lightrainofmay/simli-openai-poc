# 英语口语语伴 · LiveKit 实时数字人

基于现有“小语桥”底层链路改造：  
**LiveKit Agents + Deepgram STT + Cartesia TTS + OpenAI/OpenRouter + Simli Avatar**。

目标人群：**中国学生英语口语练习**（实时语伴、短句纠错、场景对练）。

## 快速启动

```bash
cd english_speaking_buddy_cn
python3 -m pip install -r requirements.txt
cp .env.livekit.example .env.livekit
# 编辑 .env.livekit：填入 LiveKit / Simli / Deepgram / Cartesia / LLM 密钥
python3 livekit_simli_agent.py download-files
./start_all.sh
```

- 页面地址：<http://127.0.0.1:8030>
- 日志：`.logs/agent.log`、`.logs/realtime.log`
- 停止：`./stop_all.sh`

## 关键改造点

- Agent 角色从“中文学伴”改为“英语口语语伴”
- STT 默认语言改为 `en-US`
- TTS 默认语言改为 `en`
- Web 场景改为校园/面试/出行英语口语练习
- `/api/translate` 从老挝语字幕改为**中文释义**

## 目录说明

- `livekit_simli_agent.py`: LiveKit Worker，语音理解与语伴回复逻辑
- `realtime_server.py`: FastAPI 服务，提供页面、token、翻译 API
- `realtime_web/index.html`: 实时网页客户端
- `start_all.sh` / `stop_all.sh`: 本地一键启停
- `Dockerfile.web`: 仅部署网页 + API（Agent 另行部署）
