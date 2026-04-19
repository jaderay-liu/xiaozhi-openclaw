# 架构说明

## 整体链路

```
 ┌─────────────┐                          ┌──────────────────────────────┐
 │  小智硬件    │   ── Opus 音频帧 ──▶    │       xiaozhi-openclaw       │
 │ (ESP32)     │                          │      (本服务 FastAPI)         │
 │             │   ◀── Opus 音频流 ──     │                              │
 │  麦克风 📢  │                          │  ┌──────────────────────┐    │
 │  扬声器 🔊  │   ─── JSON 控制帧 ──▶    │  │  WebSocket 会话管理  │    │
 │  屏幕  🖥️  │                          │  │  /xiaozhi/v1/        │    │
 └─────────────┘                          │  └─────────┬────────────┘    │
                                          │            │                 │
                                          │    ┌───────┴────────┐        │
                                          │    ▼                ▼        │
                                          │  ┌─────┐        ┌──────┐     │
                                          │  │ ASR │        │ MCP  │     │
                                          │  └──┬──┘        └──────┘     │
                                          │     │                        │
                                          │     ▼ 文本                    │
                                          │  ┌────────────┐              │
                                          │  │ OpenClaw   │              │
                                          │  │ Session    │              │
                                          │  └─────┬──────┘              │
                                          │        │ AI 回复              │
                                          │        ▼                      │
                                          │  ┌────────┐                  │
                                          │  │  TTS   │                  │
                                          │  └────────┘                  │
                                          └──────────────────────────────┘
                                                   │           ▲
                                                   ▼           │
                                          ┌─────────────────────────────┐
                                          │   火山引擎云 (OpenSpeech)   │
                                          │   ASR / TTS WebSocket        │
                                          └─────────────────────────────┘

                                                   │           ▲
                                                   ▼           │
                                          ┌─────────────────────────────┐
                                          │   OpenClaw Gateway          │
                                          │   ws://127.0.0.1:18789/ws   │
                                          └─────────────────────────────┘
```

## 一次完整对话的时间线

| 步骤 | 方向 | 内容 |
|------|------|------|
| 1 | 硬件 → 服务 | 建立 WebSocket，发 `hello` 包（含 `device_id`） |
| 2 | 服务 → 硬件 | 回 `hello` 握手 |
| 3 | 服务 → 火山 ASR | 建立 ASR WebSocket，发 config 和 OGG 头页 |
| 4 | 服务 → OpenClaw | 建立 Gateway WebSocket，完成签名握手 |
| 5 | 服务 → OpenClaw | 首次发 SYSTEM_PROMPT（只注入一次） |
| 6 | 硬件 → 服务 | 用户按下按键，开始推流 Opus 音频帧 |
| 7 | 服务 → 火山 ASR | 实时转发 Opus 音频（封装为 OGG 页） |
| 8 | 火山 ASR → 服务 | 流式返回 ASR 文本（中间结果 + 最终结果） |
| 9 | VAD 触发 | 静音 1.5s 或结果稳定 1.5s 发 EOS 给 ASR |
| 10 | 服务 → 硬件 | 发 `stt` 文本（屏幕显示用户说的话） |
| 11 | 服务 → OpenClaw | `chat.send` 发送 ASR 文本 |
| 12 | OpenClaw → 服务 | 流式返回 AI 回复（`assistant.delta` → `lifecycle.end`） |
| 13 | 服务 → 硬件 | 发 `tts.start` + `tts.sentence_start`（屏幕显示 AI 回复） |
| 14 | 服务 → 火山 TTS | 建立 TTS WebSocket，发文本 |
| 15 | 火山 TTS → 服务 | 流式返回 MP3 数据块 |
| 16 | 服务内部 | MP3 → PCM（miniaudio）→ Opus（libopus）编码 |
| 17 | 服务 → 硬件 | 推 Opus 帧（按 55ms 节拍），硬件播放 |
| 18 | 服务 → 硬件 | 发 `tts.stop`，本轮结束 |

## 关键设计决策

### 为什么 OpenClaw 是长连接？

每次对话都握手一次开销太大（连接 + 鉴权约 300-500ms）。长连接 + 自动重连策略把这个延迟消除，同时 session_key 绑定 device_id 能跨连接保留记忆。

### 为什么需要纯 Python 打包 OGG/Opus？

火山 ASR 只接受 OGG 容器包装的 Opus 流，但小智硬件推上来的是裸 Opus 帧。为了避免依赖系统 `ogg` 工具，我们在 Python 里实现了一套 OGG 页封装（`_ogg_page` / `_ogg_crc`）。

### 为什么 TTS 用 MP3 而不是直接用 Opus？

火山 TTS 的 Opus 输出采样率固定 24k，跟硬件期望的 16k 不一致。用 MP3 作为中间格式，我们可以在 `miniaudio` 解码阶段直接指定 `sample_rate=16000`，简化重采样。

### MCP 音量控制协议

音量调节通过小智硬件支持的 MCP (Model Context Protocol) JSON-RPC 2.0 完成：

```json
{
    "type": "mcp",
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "self.audio_speaker.set_volume",
        "arguments": {"volume": 50}
    },
    "id": 1001
}
```

硬件侧自动调用本地的 `audio_speaker.set_volume` 工具。

### [DISCONNECT] 语义断链

传统做法是在服务端硬编码关键词（"再见""拜拜"）匹配后断开，容易漏判或误伤。改用让 AI 自己判断 —— 系统提示词告诉它告别时在回复末尾输出 `[DISCONNECT]`，服务端解析这个 token 决定是否断开。

## 状态管理

所有会话状态都用以 `client_id`（uuid4）为 key 的 dict 维护：

| 字典 | 存什么 |
|------|--------|
| `clients` | 设备 WebSocket |
| `asr_clients` | `VolcASR` 实例 |
| `oc_sessions` | `OpenClawSession` 实例 |
| `processing_flags` | 当前是否正在处理 ASR 结果（防重入） |
| `last_asr_texts` | 最近一次 ASR 中间结果（用于稳定性判定） |
| `silence_timers` | 静音超时计时器 |
| `result_stable_timers` | 结果稳定超时计时器 |

连接断开时在 `finally` 里统一清理。

## 并发模型

完全基于 asyncio 单进程事件循环：
- FastAPI/Uvicorn 托管 WebSocket 会话
- 每个会话启动一个 `asr_recv_loop` 协程（`asyncio.ensure_future`）
- 长时间任务（TTS 合成 + 推流）用 `asyncio.ensure_future(process_asr_result)` 异步执行
- 所有 WebSocket 发送都包了 try/except（`_ws_send_text` / `_ws_send_bytes`），设备中途断开不会抛异常
