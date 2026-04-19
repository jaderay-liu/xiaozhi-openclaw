#!/usr/bin/env python3
"""
小智 AI 服务端 —— 完整链路
硬件 → ASR(火山) → OpenClaw AI → TTS(火山) → 硬件

依赖:
  pip install -r requirements.txt
  apt-get install -y libopus0 libopus-dev

功能特性:
  * 屏幕显示修复：发 tts/sentence_start 让硬件屏幕显示 AI 回复文本
  * AI 语义断链：[DISCONNECT] 指令，AI 自主判断是否结束对话
  * websockets 版本兼容：14+（close_code is None）和 13-（closed）都支持
  * OpenClaw 长连接 + device_id 绑定（关机重开记忆不丢）+ 系统提示词
  * 音量控制：MCP JSON-RPC 2.0 协议，语音指令直接调节硬件音量
  * 清除记忆：通过关键词触发，重置当前设备的对话上下文
"""

import asyncio
import base64
import gzip
import json
import logging
import os
import re
import struct
import sys
import time
import uuid

import aiofiles
import uvicorn
import websockets
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# ================================================================
#  加载配置
# ================================================================
try:
    import config  # type: ignore
except ModuleNotFoundError:
    print(
        '\n[FATAL] 未找到 config.py，请先执行:\n'
        '    cp config.example.py config.py\n'
        '然后按实际情况修改里面的配置项（或通过环境变量覆盖）\n',
        file=sys.stderr,
    )
    sys.exit(1)


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
app = FastAPI()


# ================================================================
#  OpenClaw 身份文件加载
# ================================================================
try:
    with open(config.OC_DEVICE_JSON) as f:
        _dev = json.load(f)
    with open(config.OC_DEVICE_AUTH_JSON) as f:
        _dev_auth = json.load(f)
    with open(config.OC_PAIRED_JSON) as f:
        _paired = json.load(f)
except FileNotFoundError as e:
    logger.error(f'[OC INIT] 找不到 OpenClaw 身份文件: {e}')
    logger.error('[OC INIT] 请先按 README 的指引完成 OpenClaw 配对')
    sys.exit(1)

OC_DEVICE_ID      = _dev['deviceId']
OC_PRIVATE_KEY    = load_pem_private_key(_dev['privateKeyPem'].encode(), password=None)
OC_PUBLIC_KEY_B64 = _paired[OC_DEVICE_ID]['publicKey']
OC_AUTH_TOKEN     = _dev_auth['tokens']['operator']['token']


# ================================================================
#  协议常量
# ================================================================
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST  = 0b0010
POS_SEQUENCE        = 0b0001
NEG_WITH_SEQUENCE   = 0b0011

clients              = {}
asr_clients          = {}
result_stable_timers = {}
last_asr_texts       = {}
silence_timers       = {}
processing_flags     = {}
oc_sessions          = {}   # conn_id → OpenClawSession

_oc_msg_id = 0

def _oc_next_id() -> str:
    global _oc_msg_id
    _oc_msg_id += 1
    return str(_oc_msg_id)


# ================================================================
#  OpenClaw 签名 & 握手
# ================================================================
def _oc_sign(payload: str) -> str:
    return base64.urlsafe_b64encode(
        OC_PRIVATE_KEY.sign(payload.encode('utf-8'))
    ).decode().rstrip('=')


def _oc_make_connect(nonce: str, challenge_ts: int = None) -> dict:
    now         = challenge_ts or int(time.time() * 1000)
    client_id   = 'cli'
    client_mode = 'cli'
    role        = 'operator'
    scopes_str  = ','.join(config.OC_SCOPES)

    if nonce:
        payload = f'v2|{OC_DEVICE_ID}|{client_id}|{client_mode}|{role}|{scopes_str}|{now}|{OC_AUTH_TOKEN}|{nonce}'
    else:
        payload = f'v1|{OC_DEVICE_ID}|{client_id}|{client_mode}|{role}|{scopes_str}|{now}|{OC_AUTH_TOKEN}'

    return {
        'type': 'req', 'id': _oc_next_id(), 'method': 'connect',
        'params': {
            'minProtocol': 3, 'maxProtocol': 3,
            'client': {'id': client_id, 'mode': client_mode,
                       'version': '1.0.0', 'platform': 'linux'},
            'role': role, 'scopes': config.OC_SCOPES,
            'caps': [], 'commands': [], 'permissions': {},
            'auth': {'token': OC_AUTH_TOKEN},
            'device': {
                'id': OC_DEVICE_ID, 'publicKey': OC_PUBLIC_KEY_B64,
                'signature': _oc_sign(payload),
                'signedAt': now, 'nonce': nonce,
            },
            'locale': 'zh-CN',
        },
    }


# ================================================================
#  websockets 版本兼容
# ================================================================
def _ws_is_open(ws) -> bool:
    """兼容 websockets 13-（closed）和 14+（close_code）"""
    if ws is None:
        return False
    if hasattr(ws, 'close_code'):   # websockets 14+
        return ws.close_code is None
    if hasattr(ws, 'closed'):       # websockets 13-
        return not ws.closed
    return False


# ================================================================
#  OpenClaw 长连接 Session
# ================================================================
class OpenClawSession:
    def __init__(self, conn_id: str, device_id: str):
        self.conn_id      = conn_id
        self.device_id    = device_id
        # session_key 绑定硬件 device_id，关机重开后不变，对话记忆保留
        self.session_key  = f'agent:main:{device_id}'
        self.ws           = None
        self._connected   = False
        self._lock        = asyncio.Lock()
        self._initialized = False   # 是否已注入系统提示词

    async def _connect(self):
        try:
            self.ws = await websockets.connect(
                config.OC_GATEWAY_URL, ping_interval=None
            )
        except Exception as e:
            logger.error(f'[OC] 连接失败: {e}')
            raise

        first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
        nonce = challenge_ts = None
        if first.get('event') == 'connect.challenge':
            nonce        = first['payload'].get('nonce', '')
            challenge_ts = first['payload'].get('ts')

        await self.ws.send(json.dumps(_oc_make_connect(nonce or '', challenge_ts)))

        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            if resp.get('type') == 'event' and resp.get('event') == 'connect.challenge':
                continue
            break

        if not resp.get('ok'):
            raise RuntimeError(f'[OC] 握手失败: {resp.get("error")}')

        self._connected = True
        logger.info(f'[OC] 长连接建立成功 device={self.device_id}')

    async def _ensure(self):
        if self._connected and _ws_is_open(self.ws):
            return
        self._connected   = False
        self._initialized = False
        logger.info(f'[OC] 重新连接 device={self.device_id}')
        await self._connect()

    async def ask(self, text: str) -> str:
        async with self._lock:
            for attempt in range(2):
                try:
                    await self._ensure()
                    return await self._do_ask(text)
                except (websockets.ConnectionClosed, asyncio.TimeoutError,
                        OSError, RuntimeError) as e:
                    logger.warning(f'[OC] 连接中断，重试 attempt={attempt}: {e}')
                    self._connected   = False
                    self._initialized = False
                    if attempt == 1:
                        logger.error('[OC] 重试也失败，放弃')
                        return ''

    async def _do_ask(self, text: str) -> str:
        # 首次对话注入系统提示词
        if not self._initialized:
            self._initialized = True
            logger.info(f'[OC] 注入系统提示词 device={self.device_id}')
            sys_id = _oc_next_id()
            await self.ws.send(json.dumps({
                'type': 'req', 'id': sys_id, 'method': 'chat.send',
                'params': {
                    'sessionKey':     self.session_key,
                    'message':        config.SYSTEM_PROMPT,
                    'idempotencyKey': f'sys-{int(time.time() * 1000)}',
                    'attachments':    [],
                },
            }))
            # 等系统提示词这轮结束
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get('type') == 'event' and msg.get('event') == 'agent':
                    data = msg.get('payload', {})
                    if data.get('sessionKey') != self.session_key:
                        continue
                    if data.get('stream') == 'lifecycle':
                        phase = data.get('data', {}).get('phase')
                        if phase in ('end', 'error'):
                            break
            logger.info('[OC] 系统提示词注入完成')

        # 正式问答
        req_id = _oc_next_id()
        await self.ws.send(json.dumps({
            'type': 'req', 'id': req_id, 'method': 'chat.send',
            'params': {
                'sessionKey':     self.session_key,
                'message':        text,
                'idempotencyKey': f'msg-{int(time.time() * 1000)}',
                'attachments':    [],
            },
        }))

        parts = []
        async for raw in self.ws:
            msg   = json.loads(raw)
            mtype = msg.get('type')

            if mtype == 'res' and msg.get('id') == req_id:
                if not msg.get('ok'):
                    logger.error(f'[OC] chat.send 失败: {msg.get("error")}')
                    return ''
                continue

            if mtype == 'event' and msg.get('event') == 'agent':
                data   = msg.get('payload', {})
                if data.get('sessionKey') != self.session_key:
                    continue
                stream = data.get('stream')
                inner  = data.get('data', {})

                if stream == 'assistant':
                    delta = inner.get('delta') or ''
                    if delta:
                        parts.append(delta)
                elif stream == 'lifecycle':
                    phase = inner.get('phase')
                    if phase == 'end':
                        break
                    elif phase == 'error':
                        logger.error(f'[OC] lifecycle error: {inner}')
                        break

        result = ''.join(parts)
        logger.info(f'[OC] AI 回复: {result}')
        return result

    def reset_memory(self):
        """换新 session_key，相当于重新开始对话"""
        self.session_key  = f'agent:main:{self.device_id}-{int(time.time())}'
        self._initialized = False
        logger.info(f'[OC] 记忆已重置 new_key={self.session_key}')

    async def close(self):
        self._connected = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        logger.info(f'[OC] 长连接关闭 device={self.device_id}')


# ================================================================
#  OGG/Opus 纯 Python 打包
# ================================================================
_CRC_TABLE = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04c11db7) if _r & 0x80000000 else (_r << 1)
        _r &= 0xFFFFFFFF
    _CRC_TABLE.append(_r)

def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC_TABLE[(crc >> 24) ^ b]) & 0xFFFFFFFF
    return crc

def _ogg_page(granule, serial, seqno, payload, bos=False, eos=False) -> bytes:
    htype = (0x02 if bos else 0) | (0x04 if eos else 0)
    segs, n = [], len(payload)
    while True:
        s = min(255, n); segs.append(s); n -= s
        if s < 255: break
    header = (b'OggS\x00' + bytes([htype])
              + struct.pack('<q', granule)
              + struct.pack('<I', serial)
              + struct.pack('<I', seqno)
              + b'\x00\x00\x00\x00'
              + bytes([len(segs)]) + bytes(segs))
    page = header + payload
    return page[:22] + struct.pack('<I', _ogg_crc(page)) + page[26:]


# ================================================================
#  火山 ASR 协议层
# ================================================================
def _hdr(msg_type, flags, serialization=0, compression=0):
    return bytes([(1 << 4) | 1, (msg_type << 4) | flags,
                  (serialization << 4) | compression, 0x00])

def _seq(n): return struct.pack('>i', n)

def parse_asr_response(data: bytes) -> dict:
    if len(data) < 4: return {}
    msg_type    = (data[1] >> 4) & 0x0F
    flags       = data[1] & 0x0F
    compression = data[2] & 0x0F
    has_seq     = bool(flags & 0x01)

    if msg_type == 0x0F:
        error_code = struct.unpack('>I', data[4:8])[0]  if len(data) >= 8  else 0
        msg_len    = struct.unpack('>I', data[8:12])[0] if len(data) >= 12 else 0
        msg_text   = data[12:12+msg_len].decode('utf-8', errors='replace') if msg_len else ''
        logger.error(f'[ASR SERVER ERR] code={error_code:#010x} msg={msg_text!r}')
        return {'error': error_code, 'error_msg': msg_text}

    try:
        if has_seq:
            if len(data) < 12: return {}
            psize   = struct.unpack('>I', data[8:12])[0]
            payload = data[12:12+psize]
        else:
            if len(data) < 8: return {}
            psize   = struct.unpack('>I', data[4:8])[0]
            payload = data[8:8+psize]
        if not payload: return {}
        if compression == 1: payload = gzip.decompress(payload)
        body = json.loads(payload.decode('utf-8'))
        return {'body': body, 'is_last': bool(flags & 0b0010)}
    except Exception as e:
        logger.warning(f'[ASR PARSE] {e}  hex={data[:8].hex()}')
        return {}


class VolcASR:
    OGG_SERIAL = 0xA1B2C3D4
    FRAME_MS   = 60
    GPC        = 48000 * FRAME_MS // 1000

    def __init__(self, client_id: str):
        self.client_id   = client_id
        self.ws          = None
        self.seq         = 0
        self.ogg_seqno   = 0
        self.granule     = 0
        self.running     = False
        self.frame_count = 0

    async def _send_raw(self, msg_type, flags, payload: bytes, last: bool = False):
        self.seq += 1
        seq  = -self.seq if last else self.seq
        comp = 1 if msg_type == FULL_CLIENT_REQUEST else 0
        data = payload if comp == 0 else gzip.compress(payload)
        await self.ws.send(
            _hdr(msg_type, flags, compression=comp) +
            _seq(seq) +
            struct.pack('>I', len(data)) +
            data
        )

    async def _send_ogg_page(self, page: bytes, last: bool = False):
        flags = NEG_WITH_SEQUENCE if last else POS_SEQUENCE
        await self._send_raw(AUDIO_ONLY_REQUEST, flags, page, last=last)

    async def connect(self):
        self.ws = await websockets.connect(
            config.ASR_WS_URL,
            additional_headers=[
                ('X-Api-App-Key',     config.VOLC_APP_KEY),
                ('X-Api-Access-Key',  config.VOLC_ACCESS_KEY),
                ('X-Api-Resource-Id', config.VOLC_RESOURCE_ID),
                ('X-Api-Connect-Id',  self.client_id),
            ],
            ping_interval=None, ping_timeout=None,
        )
        self.running = True
        self.seq     = 0

        config_payload = gzip.compress(json.dumps({
            'audio':   {'format': 'ogg', 'codec': 'opus',
                        'rate': 16000, 'channel': 1},
            'request': {'model_name': 'bigmodel',
                        'enable_itn': True, 'enable_punc': True},
        }).encode())
        self.seq += 1
        await self.ws.send(
            _hdr(FULL_CLIENT_REQUEST, POS_SEQUENCE, serialization=1, compression=1) +
            _seq(self.seq) +
            struct.pack('>I', len(config_payload)) +
            config_payload
        )
        logger.info(f'[ASR] 已连接 {self.client_id[:8]}')

        opus_head = (b'OpusHead\x01'
                     + bytes([1])
                     + struct.pack('<H', 312)
                     + struct.pack('<I', 16000)
                     + struct.pack('<H', 0)
                     + b'\x00')
        opus_tags = (b'OpusTags'
                     + struct.pack('<I', 6) + b'Python'
                     + struct.pack('<I', 0))
        head_page = _ogg_page(-1, self.OGG_SERIAL, 0, opus_head, bos=True)
        tags_page = _ogg_page(0,  self.OGG_SERIAL, 1, opus_tags)
        self.ogg_seqno = 2
        self.granule   = 0

        await self._send_ogg_page(head_page + tags_page, last=False)
        logger.info(f'[ASR] OGG 头页已发 {self.client_id[:8]}')

    async def send_frame(self, opus_frame: bytes, last: bool = False):
        if not self.ws or not self.running: return
        self.granule += self.GPC
        self.frame_count += 1
        page = _ogg_page(self.granule, self.OGG_SERIAL, self.ogg_seqno,
                         opus_frame, eos=last)
        self.ogg_seqno += 1
        await self._send_ogg_page(page, last=last)
        if last:
            self.running = False
            logger.info(f'[ASR] EOS 已发，共 {self.frame_count} 帧')

    async def finish(self):
        if not self.ws or not self.running: return
        self.granule += self.GPC
        page = _ogg_page(self.granule, self.OGG_SERIAL, self.ogg_seqno,
                         b'\xf8\xff\xfe', eos=True)
        self.ogg_seqno += 1
        await self._send_ogg_page(page, last=True)
        self.running = False
        logger.info(f'[ASR] finish EOS 已发，共 {self.frame_count} 帧')

    async def close(self):
        self.running = False
        if self.ws:
            try: await self.ws.close()
            except: pass


# ================================================================
#  ASR 接收循环
# ================================================================
async def asr_recv_loop(client_id: str, asr: VolcASR):
    try:
        async for message in asr.ws:
            if not isinstance(message, bytes): continue
            result = parse_asr_response(message)
            if not result: continue
            if 'error' in result: continue
            body    = result.get('body', {})
            code    = body.get('code', 0)
            is_last = result.get('is_last', False)
            text    = body.get('result', {}).get('text', '')
            if code != 0:
                logger.error(f'[ASR ERR {code}] {body.get("message", "")}')
                continue
            if text:
                logger.info(f'[ASR] {"★最终" if is_last else "中间"}: {text}')
            if is_last and text:
                _cancel_result_timer(client_id)
                if not processing_flags.get(client_id, False):
                    asyncio.ensure_future(process_asr_result(client_id, text))
            elif text:
                prev = last_asr_texts.get(client_id, '')
                last_asr_texts[client_id] = text
                if text != prev:
                    _reset_result_timer(client_id, asr, text)
    except Exception as e:
        logger.info(f'[ASR LOOP END] {e}')


# ================================================================
#  计时器
# ================================================================
def _cancel_result_timer(client_id: str):
    t = result_stable_timers.pop(client_id, None)
    if t: t.cancel()

def _reset_result_timer(client_id: str, asr: VolcASR, text: str):
    _cancel_result_timer(client_id)
    async def _run():
        try:
            await asyncio.sleep(config.RESULT_STABLE_TIMEOUT)
            result_stable_timers.pop(client_id, None)
            current = last_asr_texts.get(client_id, '')
            if current == text and asr.running:
                logger.info(f'[VAD] 结果已稳定 {config.RESULT_STABLE_TIMEOUT}s（"{text}"），发 EOS')
                await asr.finish()
        except asyncio.CancelledError: pass
        except Exception as e: logger.error(f'[VAD ERR] {e}')
    result_stable_timers[client_id] = asyncio.ensure_future(_run())

def _cancel_timer(client_id: str):
    t = silence_timers.pop(client_id, None)
    if t: t.cancel()

def _reset_timer(client_id: str):
    _cancel_timer(client_id)
    async def _run():
        try:
            await asyncio.sleep(config.SILENCE_TIMEOUT)
            silence_timers.pop(client_id, None)
            logger.info(f'[VAD] {config.SILENCE_TIMEOUT}s 静音超时，发 EOS')
            asr = asr_clients.get(client_id)
            if asr: await asr.finish()
        except asyncio.CancelledError: pass
        except Exception as e: logger.error(f'[VAD ERR] {e}')
    silence_timers[client_id] = asyncio.ensure_future(_run())


# ================================================================
#  ASR 重建
# ================================================================
async def _rebuild_asr(client_id: str):
    old = asr_clients.pop(client_id, None)
    if old: await old.close()
    last_asr_texts.pop(client_id, None)
    new_asr = VolcASR(client_id)
    try:
        await new_asr.connect()
        asr_clients[client_id] = new_asr
        asyncio.ensure_future(asr_recv_loop(client_id, new_asr))
        logger.info(f'[ASR] 重建完成 {client_id[:8]}')
    except Exception as e:
        logger.error(f'[ASR RECONNECT ERR] {e}')


# ================================================================
#  辅助发送
# ================================================================
async def _ws_send_text(ws, data: str) -> bool:
    try:    await ws.send_text(data); return True
    except: return False

async def _ws_send_bytes(ws, data: bytes) -> bool:
    try:    await ws.send_bytes(data); return True
    except: return False


# ================================================================
#  清除记忆关键词
# ================================================================
_CLEAR_MEMORY_KW = ['清除记忆', '忘掉我', '重新开始', '清空对话', '忘记我']


# ================================================================
#  音量控制（MCP JSON-RPC 2.0 协议）
# ================================================================
_VOLUME_KW = ['音量调', '音量设', '音量改', '调音量', '设音量', '声音调',
              '音量到', '调到', '大声点', '小声点', '静音', '最大音量', '最小音量']

def _parse_volume(text: str):
    """从 ASR 文本里提取 0-100 的音量值，失败返回 None"""
    # 优先匹配百分比数字，例如 "50%" "50％"
    m = re.search(r'(\d{1,3})\s*[%％]', text)
    if m:
        return max(0, min(100, int(m.group(1))))
    # 纯数字兜底，例如 "音量调到50"
    m = re.search(r'(\d{1,3})', text)
    if m:
        v = int(m.group(1))
        if 0 <= v <= 100:
            return v
    # 语义关键词
    if '静音' in text:
        return 0
    if '最大' in text or '最响' in text:
        return 100
    if '最小' in text or '最低' in text:
        return 10
    return None

_mcp_req_id = 1000

def _mcp_next_id() -> int:
    global _mcp_req_id
    _mcp_req_id += 1
    return _mcp_req_id


# ================================================================
#  主业务流程：ASR → OpenClaw AI → TTS → 推流
# ================================================================
async def process_asr_result(client_id: str, asr_text: str):
    if processing_flags.get(client_id, False):
        logger.info(f'[FLOW] 已有处理中任务，跳过: {asr_text}')
        return
    processing_flags[client_id] = True

    cur_asr = asr_clients.get(client_id)
    if cur_asr: cur_asr.running = False

    try:
        ws = clients.get(client_id)
        if not ws:
            logger.info(f'[FLOW] 设备已断开: {asr_text}')
            return

        # 发 STT 给设备（屏幕显示用户说的话）
        if not await _ws_send_text(ws, json.dumps({'type': 'stt', 'text': asr_text})):
            logger.info('[FLOW] 设备已断开（stt 发送失败）')
            return

        logger.info(f'[FLOW] 用户说: {asr_text}')

        oc_session = oc_sessions.get(client_id)
        if not oc_session:
            logger.warning('[FLOW] 找不到 OC Session，跳过')
            return

        # ── 清除记忆指令 ────────────────────────────────────
        if any(kw in asr_text for kw in _CLEAR_MEMORY_KW):
            oc_session.reset_memory()
            reply = '好的，我已经忘记之前的对话了，我们重新开始吧。'
            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'start'}))
            await _ws_send_text(ws, json.dumps({
                'type': 'tts', 'state': 'sentence_start', 'text': reply
            }))
            mp3_data = await volc_tts_synthesize(reply)
            if mp3_data:
                opus_frames = await mp3_to_opus_frames(mp3_data)
                for frame in opus_frames:
                    if client_id not in clients: break
                    await _ws_send_bytes(ws, frame)
                    await asyncio.sleep(0.055)
            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'stop'}))
            return

        # ── 音量控制指令 ────────────────────────────────────
        if any(kw in asr_text for kw in _VOLUME_KW):
            vol = _parse_volume(asr_text)
            if vol is not None:
                mcp_msg = {
                    'type':    'mcp',
                    'jsonrpc': '2.0',
                    'method':  'tools/call',
                    'params':  {
                        'name':      'self.audio_speaker.set_volume',
                        'arguments': {'volume': vol},
                    },
                    'id': _mcp_next_id(),
                }
                await _ws_send_text(ws, json.dumps(mcp_msg))
                logger.info(f'[FLOW] 已发 MCP set_volume={vol}')
                reply = f'好的，音量已调到百分之{vol}。'
            else:
                reply = '请告诉我要调到多少，比如说"音量调到50%"。'

            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'start'}))
            await _ws_send_text(ws, json.dumps({
                'type': 'tts', 'state': 'sentence_start', 'text': reply
            }))
            mp3_data = await volc_tts_synthesize(reply)
            if mp3_data:
                opus_frames = await mp3_to_opus_frames(mp3_data)
                for frame in opus_frames:
                    if client_id not in clients: break
                    await _ws_send_bytes(ws, frame)
                    await asyncio.sleep(0.055)
            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'stop'}))
            logger.info(f'[FLOW] 音量指令处理完成')
            return

        # ── 调用 OpenClaw ───────────────────────────────────
        ai_text = await oc_session.ask(asr_text)

        if not ai_text:
            logger.warning('[FLOW] AI 回复为空，跳过 TTS')
            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'stop'}))
            return

        # ── 解析 [DISCONNECT] 断链指令 ──────────────────────
        should_disconnect = '[DISCONNECT]' in ai_text
        tts_text = ai_text.replace('[DISCONNECT]', '').strip()
        logger.info(f'[FLOW] AI 回复: {ai_text}')
        if should_disconnect:
            logger.info('[FLOW] 检测到 [DISCONNECT] 指令')

        # ── 推流 ────────────────────────────────────────────
        await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'start', 'text': tts_text}))
        # 屏幕修复：sentence_start 让硬件屏幕显示 AI 回复文本
        await _ws_send_text(ws, json.dumps({
            'type': 'tts', 'state': 'sentence_start', 'text': tts_text
        }))

        try:
            _cancel_timer(client_id)
            _cancel_result_timer(client_id)

            mp3_data = await volc_tts_synthesize(tts_text)
            if mp3_data:
                opus_frames = await mp3_to_opus_frames(mp3_data)
                logger.info(f'[FLOW] 推流 {len(opus_frames)} 帧给设备')
                for frame in opus_frames:
                    if client_id not in clients: break
                    if not await _ws_send_bytes(ws, frame):
                        logger.info('[FLOW] 推流中断（设备断开）')
                        break
                    await asyncio.sleep(0.055)
            else:
                logger.warning('[FLOW] TTS 返回空音频')
        except Exception as e:
            logger.error(f'[TTS/PUSH ERR] {e}')
        finally:
            await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'stop'}))
            logger.info('[FLOW] 本轮结束')

            # ── AI 主动断开 ──────────────────────────────────
            if should_disconnect:
                logger.info('[FLOW] AI 发出断链指令，0.5s 后断开')
                await asyncio.sleep(0.5)
                try:
                    ws2 = clients.get(client_id)
                    if ws2: await ws2.close()
                except Exception:
                    pass

    finally:
        processing_flags[client_id] = False


# ================================================================
#  TTS（火山双向流式）
# ================================================================
_TTS_EV_START_CONN      = 1
_TTS_EV_FINISH_CONN     = 2
_TTS_EV_CONN_STARTED    = 50
_TTS_EV_START_SESSION   = 100
_TTS_EV_FINISH_SESSION  = 102
_TTS_EV_SESSION_STARTED = 150
_TTS_EV_SESSION_FINISH  = 152
_TTS_EV_SESSION_FAILED  = 153
_TTS_EV_TASK_REQUEST    = 200
_TTS_EV_TTS_RESPONSE    = 352

_TTS_FULL_REQ    = 0b0001
_TTS_AUDIO_RESP  = 0b1011
_TTS_FULL_RESP   = 0b1001
_TTS_ERROR       = 0b1111
_TTS_FLAG_EVENT  = 0b100
_TTS_NO_SERIAL   = 0b0000
_TTS_JSON_SERIAL = 0b0001
_TTS_NO_COMP     = 0b0000

def _tts_hdr(msg_type, msg_flag, serial=_TTS_NO_SERIAL, compress=_TTS_NO_COMP):
    return bytes([0x11, (msg_type << 4) | msg_flag, (serial << 4) | compress, 0])

def _tts_optional(event: int, session_id: str = None, sequence: int = None) -> bytes:
    buf = bytearray(event.to_bytes(4, 'big', signed=True))
    if session_id is not None:
        b = session_id.encode()
        buf += len(b).to_bytes(4, 'big', signed=True) + b
    if sequence is not None:
        buf += sequence.to_bytes(4, 'big', signed=True)
    return bytes(buf)

async def _tts_send(ws, header: bytes, optional: bytes = None, payload: bytes = None):
    frame = bytearray(header)
    if optional: frame += optional
    if payload:  frame += len(payload).to_bytes(4, 'big', signed=True) + payload
    await ws.send(bytes(frame))

def _tts_parse(res: bytes):
    if len(res) < 4: return None, None, None, 'too short'
    msg_type = (res[1] >> 4) & 0x0F
    msg_flag = res[1] & 0x0F
    offset   = 4
    event = session_id = audio = error_msg = None

    if msg_type in (_TTS_FULL_RESP, _TTS_AUDIO_RESP):
        if msg_flag & _TTS_FLAG_EVENT:
            event = int.from_bytes(res[offset:offset+4], 'big', signed=True); offset += 4

            def read_str():
                nonlocal offset
                sz = int.from_bytes(res[offset:offset+4], 'big'); offset += 4
                s  = res[offset:offset+sz].decode('utf-8'); offset += sz
                return s

            def read_bytes():
                nonlocal offset
                sz = int.from_bytes(res[offset:offset+4], 'big'); offset += 4
                b  = res[offset:offset+sz]; offset += sz
                return b

            if event == _TTS_EV_CONN_STARTED:
                if offset + 4 <= len(res): read_str()
            elif event in (_TTS_EV_SESSION_STARTED, _TTS_EV_SESSION_FAILED, _TTS_EV_SESSION_FINISH):
                if offset + 4 <= len(res): session_id = read_str()
                if offset + 4 <= len(res): read_str()
            elif event == _TTS_EV_TTS_RESPONSE:
                if offset + 4 <= len(res): session_id = read_str()
                if offset + 4 <= len(res): audio = read_bytes()

        if msg_type == _TTS_AUDIO_RESP and audio is None and offset + 4 <= len(res):
            sz = int.from_bytes(res[offset:offset+4], 'big'); offset += 4
            audio = res[offset:offset+sz]

    elif msg_type == _TTS_ERROR:
        err_code = int.from_bytes(res[offset:offset+4], 'big', signed=True); offset += 4
        if offset + 4 <= len(res):
            sz = int.from_bytes(res[offset:offset+4], 'big'); offset += 4
            error_msg = res[offset:offset+sz].decode('utf-8', errors='replace')
        else:
            error_msg = f'error_code={err_code}'

    return event, session_id, audio, error_msg


async def volc_tts_synthesize(text: str) -> bytes:
    if not text.strip(): return b''
    session_id = uuid.uuid4().hex
    chunks = []
    headers = {
        'X-Api-App-Key':     config.TTS_APP_ID,
        'X-Api-Access-Key':  config.TTS_TOKEN,
        'X-Api-Resource-Id': 'volc.service_type.10029',
        'X-Api-Connect-Id':  str(uuid.uuid4()),
    }
    try:
        async with websockets.connect(config.TTS_WS_URL, additional_headers=headers,
                                      ping_interval=None) as ws:
            await _tts_send(ws, _tts_hdr(_TTS_FULL_REQ, _TTS_FLAG_EVENT),
                            _tts_optional(_TTS_EV_START_CONN), b'{}')
            ev, _, _, err = _tts_parse(await ws.recv())
            if ev != _TTS_EV_CONN_STARTED:
                logger.error(f'[TTS] 连接失败 ev={ev} err={err}'); return b''
            logger.info('[TTS] 连接成功')

            await _tts_send(ws,
                _tts_hdr(_TTS_FULL_REQ, _TTS_FLAG_EVENT, serial=_TTS_JSON_SERIAL),
                _tts_optional(_TTS_EV_START_SESSION, session_id=session_id),
                json.dumps({
                    'user': {'uid': 'xiaozhi_user'},
                    'event': _TTS_EV_START_SESSION,
                    'namespace': 'BidirectionalTTS',
                    'req_params': {
                        'speaker': config.TTS_VOICE,
                        'audio_params': {'format': 'mp3', 'sample_rate': 24000},
                    }
                }).encode())
            ev, _, _, err = _tts_parse(await ws.recv())
            if ev != _TTS_EV_SESSION_STARTED:
                logger.error(f'[TTS] 会话失败 ev={ev} err={err}'); return b''
            logger.info('[TTS] 会话成功')

            await _tts_send(ws,
                _tts_hdr(_TTS_FULL_REQ, _TTS_FLAG_EVENT, serial=_TTS_JSON_SERIAL),
                _tts_optional(_TTS_EV_TASK_REQUEST, session_id=session_id),
                json.dumps({
                    'user': {'uid': 'xiaozhi_user'},
                    'event': _TTS_EV_TASK_REQUEST,
                    'namespace': 'BidirectionalTTS',
                    'req_params': {
                        'text': text, 'speaker': config.TTS_VOICE,
                        'audio_params': {'format': 'mp3', 'sample_rate': 24000},
                    }
                }).encode())

            await _tts_send(ws,
                _tts_hdr(_TTS_FULL_REQ, _TTS_FLAG_EVENT, serial=_TTS_JSON_SERIAL),
                _tts_optional(_TTS_EV_FINISH_SESSION, session_id=session_id),
                b'{}')

            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                if not isinstance(raw, bytes): continue
                ev, _, audio, err = _tts_parse(raw)
                if audio:
                    chunks.append(audio)
                    logger.info(f'[TTS] 累积 {sum(len(c) for c in chunks)}B')
                if err:
                    logger.error(f'[TTS] 错误: {err}'); break
                if ev in (_TTS_EV_SESSION_FINISH, _TTS_EV_SESSION_FAILED):
                    logger.info(f'[TTS] 会话结束 ev={ev}'); break

            try:
                await _tts_send(ws,
                    _tts_hdr(_TTS_FULL_REQ, _TTS_FLAG_EVENT, serial=_TTS_JSON_SERIAL),
                    _tts_optional(_TTS_EV_FINISH_CONN), b'{}')
            except Exception: pass

    except Exception as e:
        logger.error(f'[TTS ERR] {e}')

    result = b''.join(chunks)
    logger.info(f'[TTS] 完成 {len(result)}B')
    return result


async def mp3_to_opus_frames(mp3_data: bytes, frame_ms: int = 60,
                              sample_rate: int = 16000) -> list:
    frame_samples = sample_rate * frame_ms // 1000
    pcm = b''

    try:
        import miniaudio
        decoded = miniaudio.decode(
            mp3_data, nchannels=1, sample_rate=sample_rate,
            output_format=miniaudio.SampleFormat.SIGNED16,
        )
        pcm = bytes(decoded.samples)
        logger.info(f'[PCM] miniaudio 解码 {len(pcm)}B')
    except ImportError:
        try:
            proc = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', 'pipe:0',
                '-f', 's16le', '-ar', str(sample_rate), '-ac', '1', 'pipe:1',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            pcm, _ = await proc.communicate(mp3_data)
            logger.info(f'[PCM] ffmpeg 解码 {len(pcm)}B')
        except FileNotFoundError:
            logger.error('[PCM] 找不到 ffmpeg，请安装 miniaudio: pip install miniaudio')
            return []
    except Exception as e:
        logger.error(f'[PCM] 解码失败: {e}')
        return []

    if not pcm:
        logger.error('[PCM] 解码结果为空')
        return []

    try:
        import ctypes, ctypes.util

        _lib_name = ctypes.util.find_library('opus') or 'libopus.so.0'
        opus = ctypes.CDLL(_lib_name)

        opus.opus_encoder_create.restype  = ctypes.c_void_p
        opus.opus_encoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        opus.opus_encode.restype  = ctypes.c_int32
        opus.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        opus.opus_encoder_destroy.restype  = None
        opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

        OPUS_APPLICATION_VOIP = 2048
        error   = ctypes.c_int(0)
        encoder = opus.opus_encoder_create(
            ctypes.c_int(sample_rate), ctypes.c_int(1),
            ctypes.c_int(OPUS_APPLICATION_VOIP),
            ctypes.byref(error),
        )
        if not encoder or error.value != 0:
            raise RuntimeError(f'opus_encoder_create 失败: {error.value}')

        frames  = []
        max_pkt = 4000
        out_buf = (ctypes.c_ubyte * max_pkt)()

        for i in range(0, len(pcm), frame_samples * 2):
            chunk = pcm[i:i + frame_samples * 2]
            if len(chunk) < frame_samples * 2:
                chunk += b'\x00' * (frame_samples * 2 - len(chunk))
            pcm_arr = (ctypes.c_int16 * frame_samples).from_buffer_copy(chunk)
            n = opus.opus_encode(
                encoder, pcm_arr, ctypes.c_int(frame_samples),
                out_buf, ctypes.c_int32(max_pkt),
            )
            if n < 0:
                raise RuntimeError(f'opus_encode 失败: {n}')
            frames.append(bytes(out_buf[:n]))

        opus.opus_encoder_destroy(encoder)
        logger.info(f'[OPUS] {len(frames)} 帧')
        return frames

    except Exception as e:
        logger.error(f'[OPUS] 编码失败: {e}')
        return []


# ================================================================
#  HTTP / WebSocket 路由
# ================================================================
@app.post('/xiaozhi/ota/')
@app.get('/xiaozhi/ota/')
async def ota_check(request: Request):
    device_id = request.headers.get('device-id', 'unknown')
    logger.info(f'[OTA] 设备 {device_id}')
    return JSONResponse({
        'firmware':  {'version': config.CURRENT_FIRMWARE_VERSION},
        'websocket': {'url': config.WEBSOCKET_URL},
    })


@app.websocket('/xiaozhi/v1/')
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id  = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    clients[client_id]          = websocket
    processing_flags[client_id] = False

    logger.info(f'[CONNECT] {client_id[:8]}')

    # ── 解析 hello，找硬件 device_id ──────────────────────────
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        hello = json.loads(first)

        logger.info(f'[HELLO FULL] {json.dumps(hello, ensure_ascii=False)}')
        logger.info(f'[HELLO HEADERS] {dict(websocket.headers)}')

        # 尝试多种字段名，都没有就用随机 conn_id 兜底（记忆无法跨重启保留）
        device_id = (
            hello.get('device_id')
            or hello.get('mac')
            or hello.get('deviceId')
            or hello.get('client_id')
            or hello.get('serial')
            or websocket.headers.get('device-id')
            or websocket.headers.get('x-device-id')
            or websocket.headers.get('x-mac')
            or client_id
        )
        logger.info(f'[HELLO RX] raw={first}  device_id={device_id}')

        await websocket.send_text(json.dumps({
            'type': 'hello', 'transport': 'websocket', 'session_id': session_id,
            'audio_params': {'format': 'opus', 'sample_rate': 16000,
                             'channels': 1, 'frame_duration': 60},
        }))
        logger.info(f'[HELLO TX] 握手完成 {client_id[:8]}')
    except Exception as e:
        logger.error(f'[HELLO ERR] {e}')
        clients.pop(client_id, None)
        processing_flags.pop(client_id, None)
        return

    # ── 建立 OpenClaw 长连接 ──────────────────────────────────
    oc_session = OpenClawSession(client_id, device_id)
    oc_sessions[client_id] = oc_session
    try:
        await oc_session._connect()
    except Exception as e:
        logger.error(f'[OC INIT] 初始连接失败（将在首次 ask 时重试）: {e}')

    # ── 建立 ASR 连接 ─────────────────────────────────────────
    asr = VolcASR(client_id)
    try:
        await asr.connect()
        asr_clients[client_id] = asr
        asyncio.ensure_future(asr_recv_loop(client_id, asr))
    except Exception as e:
        logger.error(f'[ASR CONNECT ERR] {e}')

    # ── 主消息循环 ──────────────────────────────────────────
    try:
        while True:
            msg = await websocket.receive()
            if msg.get('type') == 'websocket.disconnect':
                logger.info(f'[DISCONNECT] {client_id[:8]}')
                break
            if msg.get('bytes'):
                await handle_audio_frame(client_id, msg['bytes'])
            elif msg.get('text'):
                await handle_json_message(client_id, msg['text'])
    except WebSocketDisconnect:
        pass
    finally:
        _cancel_timer(client_id)
        _cancel_result_timer(client_id)
        last_asr_texts.pop(client_id, None)
        processing_flags.pop(client_id, None)
        clients.pop(client_id, None)

        # 关闭 OpenClaw 长连接
        _oc = oc_sessions.pop(client_id, None)
        if _oc: await _oc.close()

        _asr = asr_clients.pop(client_id, None)
        if _asr and _asr.running:
            try:    await _asr.finish()
            except: pass
            await asyncio.sleep(2)
        if _asr: await _asr.close()
        logger.info(f'[DONE] {client_id[:8]}')


# ================================================================
#  音频帧 / JSON 消息处理
# ================================================================
async def handle_audio_frame(client_id: str, audio_data: bytes):
    asr = asr_clients.get(client_id)
    if not asr or not asr.running: return
    frame_count = asr.frame_count + 1
    if frame_count % 20 == 0:
        logger.info(f'[AUDIO] 已推流 {frame_count} 帧')
    await asr.send_frame(audio_data, last=False)
    _reset_timer(client_id)


async def handle_json_message(client_id: str, text: str):
    try:
        msg      = json.loads(text)
        msg_type = msg.get('type', '')
        state    = msg.get('state', '-')
        logger.info(f'[MSG] type={msg_type} state={state}')
        ws = clients.get(client_id)

        if msg_type == 'listen':
            if state == 'detect':
                _cancel_timer(client_id)
                if ws:
                    await ws.send_text(json.dumps(
                        {'type': 'listen', 'state': 'start', 'mode': 'auto'}
                    ))
                logger.info('[MSG] detect → 已回 listen:start，重建 ASR')
                await _rebuild_asr(client_id)

            elif state == 'start':
                _cancel_timer(client_id)
                cur_asr = asr_clients.get(client_id)
                if not cur_asr or not cur_asr.running:
                    logger.info('[MSG] start → ASR 不可用，重建连接')
                    await _rebuild_asr(client_id)
                else:
                    logger.info('[MSG] start → ASR 可用，等待音频帧')

            elif state == 'stop':
                _cancel_timer(client_id)
                logger.info('[MSG] stop → 立即发 EOS')
                asr = asr_clients.get(client_id)
                if asr and asr.running:
                    await asr.finish()

        elif msg_type == 'abort':
            logger.info('[MSG] 设备中断')
            _cancel_timer(client_id)
            _cancel_result_timer(client_id)
            if ws:
                await _ws_send_text(ws, json.dumps({'type': 'tts', 'state': 'stop'}))

    except Exception as e:
        logger.error(f'[MSG ERR] {e}')


if __name__ == '__main__':
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level='info')
