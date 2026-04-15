# 小语桥 · LiveKit 实时数字人

单一路线：**LiveKit Agents + Deepgram STT + Cartesia TTS + OpenAI（或 OpenRouter）LLM + Simli 头像**，浏览器通过 `realtime_web` 进房对话。

## 依赖

```bash
cd simli_openai_poc
python3 -m pip install -r requirements.txt
```

## 配置

```bash
cp .env.livekit.example .env.livekit
# 编辑 .env.livekit：LiveKit、Simli、Deepgram、Cartesia、OpenAI / OpenRouter 等
```

首次运行前下载 turn detector 等模型文件：

```bash
python3 livekit_simli_agent.py download-files
```

## 启动

```bash
./start_all.sh
```

- 前端：<http://127.0.0.1:8030>
- 日志：`.logs/agent.log`、`.logs/realtime.log`
- 停止：`./stop_all.sh`

## 项目结构

| 文件 | 说明 |
|------|------|
| `livekit_simli_agent.py` | LiveKit Worker：语音管道 + Simli |
| `realtime_server.py` | FastAPI：页面与 LiveKit token |
| `realtime_web/index.html` | 浏览器客户端 |
| `start_all.sh` / `stop_all.sh` | 启停 agent + realtime_server |
| `Dockerfile.web` + `requirements-web.txt` | 方案 B：仅打包网页与 Token API（Agent 在 LiveKit Cloud 部署） |

## 方案 B：公网 Web 层（Docker 示例）

在已配置好 LiveKit Cloud Agent 的前提下，本目录可单独构建 **FastAPI + 静态页**：

```bash
docker build -f Dockerfile.web -t xiaoyuqiao-web .
docker run --env-file .env.livekit -p 8030:8030 xiaoyuqiao-web
```

生产环境请在容器外再加 **HTTPS 反代**，并视需要设置 `WEB_SHOW_ADVANCED=0` 隐藏高级连接区。

## Railway 部署（网页层）

本目录已含 `railway.toml`，指定用 **`Dockerfile.web`**（避免与根目录 Agent 用 `Dockerfile` 混淆）。

1. 在 [Railway](https://railway.com/) 新建 Project → **Deploy from GitHub repo**，选中仓库。  
2. 若仓库根目录是上层文件夹（例如整个 `Playground`），在 Service **Settings → Root Directory** 填 **`simli_openai_poc`**。  
3. **Variables** 中逐项添加与本地 `.env.livekit` 相同的键值（至少包含 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`；老挝字幕需 `OPENROUTER_API_KEY` 等）。**不要**把密钥提交进 Git。  
4. Deploy 后打开 Railway 生成的 **HTTPS URL** 即可访问（平台已处理 TLS，无需自配证书）。

`Dockerfile.web` 内已使用环境变量 **`PORT`**（Railway 自动注入），本地不加 `PORT` 时仍为 **8030**。

## LiveKit Cloud 部署（概览）

LiveKit Cloud 负责两件事：**实时房间（WebRTC）** 与 **Agent Worker（本仓库的 `livekit_simli_agent.py`）**。  
**小语桥网页 + `/api/token` + `/api/translate`** 不在 LiveKit Agent 容器里，需要另选主机部署（见下文「公网 Web 层」）。

### 1. 准备

- 安装并登录 [LiveKit CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli.md)。
- 在 [LiveKit Cloud](https://cloud.livekit.io/) 创建项目，记下控制台里的 **`LIVEKIT_URL` / API Key / Secret**（给网页发 token 时还要用）。

### 2. 把 Agent 部署到 LiveKit Cloud

在项目根目录（本目录）：

```bash
lk cloud auth
# 已有多项目时：lk project list 然后 lk project set-default "<项目名>"
```

准备一份 **仅含第三方与业务变量** 的 secrets 文件（例如 `secrets.agent.env`），**不要**写入 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`——云端会为 Worker **自动注入**这三项（见 [Secrets](https://docs.livekit.io/deploy/agents/secrets.md)）。  
文件中需包含与本机 `.env.livekit` 类似的键，例如：`SIMLI_API_KEY`、`SIMLI_FACE_ID`、`DEEPGRAM_API_KEY`、`CARTESIA_API_KEY`、`CARTESIA_VOICE_ID`、以及 LLM 用的 `OPENAI_API_KEY` 或 `OPENROUTER_API_KEY` 等；`LIVEKIT_AGENT_NAME` 须与发 token 时一致（默认 `xiaoyuqiao-cloud`，与 `realtime_server.py` 里 `RoomAgentDispatch` 一致）。

首次注册并部署（会生成 `livekit.toml`，并用本仓库根目录的 **`Dockerfile`** 构建镜像）：

```bash
lk agent create --secrets-file=secrets.agent.env
```

之后改代码或依赖：

```bash
lk agent deploy
```

查看状态与日志：

```bash
lk agent status
lk agent logs
```

构建要求与镜像模板说明见官方 [Builds and Dockerfiles](https://docs.livekit.io/deploy/agents/builds.md)（镜像内需在构建阶段执行 `download-files`，根目录 `Dockerfile` 已包含）。

### 3. 公网 Web 层（页面 + Token + 老挝翻译）

用户浏览器必须能访问 **HTTPS** 下的 `realtime_server`（否则麦克风等受限）。在 Agent 已在 LiveKit Cloud 运行的前提下，用 **`Dockerfile.web`** 构建并部署到任意云主机（Railway、Fly.io、自有 VPS 等），环境变量使用 **控制台同一项目** 的 `LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`，并配置 Simli / Deepgram / Cartesia / OpenRouter 等与本地一致；需要老挝字幕时加上 `OPENROUTER_API_KEY`（及可选 `OPENROUTER_TRANSLATE_MODEL`）。

```bash
docker build -f Dockerfile.web -t xiaoyuqiao-web .
docker run --env-file .env.livekit -p 8030:8030 xiaoyuqiao-web
```

前面再加 Nginx/Caddy 等做 TLS；若把学生访问地址从 `http://127.0.0.1:8030` 换成公网域名，无需改前端代码（同域请求 `/api/*`）。

### 4. 参考链接

- [Agent deployment quickstart](https://docs.livekit.io/deploy/agents/quickstart.md)
- [Managing deployments](https://docs.livekit.io/deploy/agents/managing-deployments.md)
- [Secrets management](https://docs.livekit.io/deploy/agents/secrets.md)
