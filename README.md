# xiaozhi-openclaw

> 小智 AI 语音硬件服务端 · 接入火山 ASR / TTS + OpenClaw AI 的完整链路

把 [小智硬件](https://github.com/78/xiaozhi-esp32)（ESP32 开源 AI 语音助手）接入 **OpenClaw AI** 的服务端程序。链路完整、中文口语化、支持语义断链、音量控制，硬件关机重开对话记忆不丢。

```
硬件设备  ──Opus──▶  本服务  ──▶  火山 ASR ──▶  OpenClaw AI
   ▲                    │                           │
   └───Opus 音频流──────┴──  火山 TTS ◀────────────┘
```

---

## ✨ 功能特性

- **完整语音链路** · ASR（语音识别）→ LLM（OpenClaw）→ TTS（语音合成）全打通
- **对话记忆保留** · OpenClaw session 绑定硬件 `device_id`，关机重开记忆不丢
- **系统提示词定制** · 内置中文口语化提示词，禁用 emoji / Markdown / 括号动作
- **AI 语义断链** · AI 判断用户意图后输出 `[DISCONNECT]` 主动结束对话
- **屏幕回显** · 发送 `tts/sentence_start` 事件，硬件屏幕同步显示 AI 回复
- **音量控制** · 语音指令"音量调到 50%"通过 MCP JSON-RPC 2.0 直接控制硬件
- **清除记忆** · 说"清除记忆 / 重新开始"重置当前设备的对话上下文
- **VAD 自动断句** · 静音 1.5 秒或中间结果稳定 1.5 秒自动结束识别
- **websockets 版本兼容** · 同时支持 websockets 13- 和 14+ 两套 API

---

## 📐 架构

### 组件
| 组件 | 作用 |
|------|------|
| **小智硬件** | ESP32 设备，采集 Opus 音频，接收并播放 Opus 音频 |
| **本服务** | WebSocket 中枢，协调 ASR / OpenClaw / TTS 三条链路 |
| **火山 ASR** | 流式语音识别，输入 Opus 音频帧，输出 ASR 文本 |
| **OpenClaw** | 本地 AI 代理，提供 chat.send 长连接接口 |
| **火山 TTS** | 双向流式语音合成，输入文本，输出 MP3 |

### 数据流
1. 硬件建立 WebSocket 连接到 `/xiaozhi/v1/`，发送 hello 包（含 `device_id`）
2. 服务为该连接建立两条长连接：OpenClaw Gateway 和火山 ASR
3. 硬件推流 Opus 音频 → 服务转发给 ASR → 收到最终文本
4. 文本交给 OpenClaw → 流式拿到 AI 回复
5. AI 回复交给火山 TTS → 拿到 MP3 → 解码 PCM → 重编码 Opus → 推给硬件播放
6. 整个过程中，STT 文本、AI 回复文本通过 `stt` / `tts.sentence_start` 事件同步到硬件屏幕

---

## 🚀 快速开始

### 前置条件

- **Python** 3.9+（推荐 3.11）
- **系统库** `libopus`（Ubuntu/Debian：`apt install libopus0 libopus-dev`）
- **OpenClaw CLI** 已完成配对，`~/.openclaw/` 目录下有 `device.json` / `device-auth.json` / `paired.json`
- **火山引擎账号** 并开通了 ASR 和 TTS 服务
- 一台 [小智硬件](https://github.com/78/xiaozhi-esp32) 或兼容设备

### 1. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/xiaozhi-openclaw.git
cd xiaozhi-openclaw
```

### 2. 安装依赖

```bash
# 系统库（只需一次）
sudo apt-get install -y libopus0 libopus-dev ffmpeg

# Python 依赖
pip install -r requirements.txt
```

或者一键脚本：
```bash
bash scripts/install.sh
```

### 3. 配置

```bash
cp config.example.py config.py
```

然后编辑 `config.py`，填写以下内容：

| 配置项 | 说明 | 获取方式 |
|--------|------|---------|
| `VOLC_APP_KEY` | 火山 ASR AppKey | [火山控制台 - 语音技术](https://console.volcengine.com/speech) |
| `VOLC_ACCESS_KEY` | 火山 ASR AccessKey | 同上 |
| `TTS_APP_ID` | 火山 TTS AppID | 同上（TTS 产品） |
| `TTS_TOKEN` | 火山 TTS Token | 同上 |
| `TTS_VOICE` | 音色 ID | [音色列表](https://www.volcengine.com/docs/6561/1257544) |
| `SERVER_HOST` | 监听地址 | `0.0.0.0` 监听所有接口 |
| `SERVER_PORT` | 监听端口 | 默认 `8002` |
| `PUBLIC_HOST` | 硬件可访问的公网 IP / 域名 | 必填（给硬件下发 ws 地址用） |

也支持环境变量覆盖（优先级高于 `config.py`）：
```bash
export VOLC_APP_KEY=xxx
export TTS_TOKEN=xxx
export PUBLIC_HOST=your.domain.com
```

### 4. 启动服务

```bash
python server.py
```

看到这些日志说明启动成功：
```
INFO:     Uvicorn running on http://0.0.0.0:8002
INFO:     Application startup complete.
```

### 5. 硬件连接

小智硬件的 OTA 地址指向本服务：
```
http://YOUR_SERVER_HOST:8002/xiaozhi/ota/
```

硬件会自动拿到 WebSocket 地址、建立连接、开始对话。

---

## 🎤 语音指令

| 指令 | 效果 |
|------|------|
| "音量调到 50%" / "音量调到五十" | 通过 MCP 调节硬件音量（0–100） |
| "静音" / "最大音量" / "最小音量" | 音量语义关键词 |
| "清除记忆" / "忘掉我" / "重新开始" | 重置当前设备的 AI 对话上下文 |
| "再见" / "拜拜" / "不用了" | AI 判断后输出 `[DISCONNECT]` 主动断开 |

---

## 📁 项目结构

```
xiaozhi-openclaw/
├── server.py              # 主程序（FastAPI + WebSocket）
├── config.example.py      # 配置模板（复制成 config.py）
├── config.py              # 实际配置（已 gitignore，不会提交）
├── requirements.txt       # Python 依赖
├── scripts/
│   └── install.sh        # 一键安装脚本
├── docs/
│   └── architecture.md    # 架构详细说明
├── LICENSE                # MIT
├── .gitignore
└── README.md
```

---

## ⚙️ 关键配置说明

### OpenClaw 身份文件

服务启动时需要从 `~/.openclaw/` 读取三个文件来建立与 OpenClaw Gateway 的鉴权长连接：

- `~/.openclaw/identity/device.json` · 设备 ID + 私钥
- `~/.openclaw/identity/device-auth.json` · operator token
- `~/.openclaw/devices/paired.json` · 配对的公钥

如果你的 OpenClaw 安装在自定义路径，可以在 `config.py` 里覆盖：
```python
OC_DEVICE_JSON      = '/custom/path/device.json'
OC_DEVICE_AUTH_JSON = '/custom/path/device-auth.json'
OC_PAIRED_JSON      = '/custom/path/paired.json'
```

### session_key 记忆机制

```python
session_key = f'agent:main:{device_id}'
```

`device_id` 从硬件 hello 包里提取（依次尝试 `device_id` / `mac` / `deviceId` / `client_id` / `serial` / 多个 header 字段）。只要硬件 MAC 地址不变，关机重开后 OpenClaw 侧的对话记忆就还在。

### 系统提示词

在 `config.py` 的 `SYSTEM_PROMPT` 里修改角色设定。默认提示词要求：
- 禁用 emoji / Markdown / 括号动作
- 口语化中文，数字用中文说
- 告别时末尾输出 `[DISCONNECT]`

---

## 🔧 故障排查

### ASR 无识别结果
- 检查火山 AppKey / AccessKey 是否正确
- 检查火山控制台是否开通了对应服务
- 查看日志里的 `[ASR SERVER ERR]` 行

### TTS 无音频
- 检查 TTS AppID / Token
- 检查 `TTS_VOICE` 是否是账号已授权的音色

### OpenClaw 连接失败
- 检查 `~/.openclaw/` 目录下三个 json 文件是否齐全
- 检查 OpenClaw Gateway 是否在 `127.0.0.1:18789` 监听
- 看日志里的 `[OC] 握手失败` 具体错误

### Opus 编码失败
```
[OPUS] 编码失败: opus_encoder_create 失败
```
未安装 libopus：
```bash
sudo apt-get install -y libopus0 libopus-dev
```

---

## 🤝 贡献

欢迎 PR 和 Issue。提交 PR 前请确保：
- 不把 `config.py` / 任何 API Key 提交进去
- 代码风格保持与现有一致

---

## 📄 License

[MIT](LICENSE)

---

## 🙏 致谢

- [小智硬件项目 (xiaozhi-esp32)](https://github.com/78/xiaozhi-esp32) · 开源 ESP32 AI 语音硬件
- [OpenClaw](https://openclaw.ai) · 本地 AI 代理框架
- [火山引擎](https://www.volcengine.com) · ASR / TTS 云服务
