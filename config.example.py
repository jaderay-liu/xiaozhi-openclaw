"""
小智 AI 服务端配置文件模板

使用方法：
    1. 把本文件复制成 config.py:  cp config.example.py config.py
    2. 按实际情况填写下面的配置项
    3. config.py 已加入 .gitignore，不会被 git 跟踪
    4. 也支持通过环境变量覆盖（优先级高于本文件）
"""

import os


# ================================================================
#  火山引擎 ASR（语音识别）配置
#  控制台: https://console.volcengine.com/speech
# ================================================================
VOLC_APP_KEY     = os.getenv('VOLC_APP_KEY',     'YOUR_VOLC_APP_KEY')
VOLC_ACCESS_KEY  = os.getenv('VOLC_ACCESS_KEY',  'YOUR_VOLC_ACCESS_KEY')
VOLC_RESOURCE_ID = os.getenv('VOLC_RESOURCE_ID', 'volc.bigasr.sauc.duration')
ASR_WS_URL       = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async'


# ================================================================
#  火山引擎 TTS（语音合成）配置
# ================================================================
TTS_APP_ID = os.getenv('TTS_APP_ID', 'YOUR_TTS_APP_ID')
TTS_TOKEN  = os.getenv('TTS_TOKEN',  'YOUR_TTS_TOKEN')
TTS_WS_URL = 'wss://openspeech.bytedance.com/api/v3/tts/bidirection'
# 音色列表：https://www.volcengine.com/docs/6561/1257544
TTS_VOICE  = os.getenv('TTS_VOICE',  'zh_female_wanwanxiaohe_moon_bigtts')


# ================================================================
#  服务端监听配置
# ================================================================
# 监听的公网 IP / 域名（硬件会连这里）
SERVER_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.getenv('SERVER_PORT', '8002'))

# OTA 升级接口回给硬件的 WebSocket 地址
# 如果 SERVER_HOST=0.0.0.0，请把这里改成实际可访问的公网 IP 或域名
PUBLIC_HOST              = os.getenv('PUBLIC_HOST', SERVER_HOST)
CURRENT_FIRMWARE_VERSION = '1.0.0'
WEBSOCKET_URL            = f'ws://{PUBLIC_HOST}:{SERVER_PORT}/xiaozhi/v1/'


# ================================================================
#  静音 / 结果稳定超时（秒）
# ================================================================
SILENCE_TIMEOUT       = 1.5   # 无音频帧多久后发 EOS
RESULT_STABLE_TIMEOUT = 1.5   # ASR 中间结果不变多久后认为说完了


# ================================================================
#  OpenClaw AI 配置
#  默认从 ~/.openclaw/ 目录读取身份文件，和 openclaw CLI 保持一致
# ================================================================
OC_DEVICE_JSON      = os.path.expanduser(
    os.getenv('OC_DEVICE_JSON', '~/.openclaw/identity/device.json'))
OC_DEVICE_AUTH_JSON = os.path.expanduser(
    os.getenv('OC_DEVICE_AUTH_JSON', '~/.openclaw/identity/device-auth.json'))
OC_PAIRED_JSON      = os.path.expanduser(
    os.getenv('OC_PAIRED_JSON', '~/.openclaw/devices/paired.json'))

OC_GATEWAY_URL = os.getenv('OC_GATEWAY_URL', 'ws://127.0.0.1:18789/ws')

OC_SCOPES = [
    'operator.read', 'operator.write', 'operator.admin',
    'operator.approvals', 'operator.pairing',
]


# ================================================================
#  系统提示词（可按需修改角色设定）
# ================================================================
SYSTEM_PROMPT = """你现在运行在一台「小智」智能语音硬件设备上。
用户通过麦克风说话，你的回复会被文字转语音（TTS）直接播放给用户听。

请严格遵守以下输出规范：
1. 【禁止】使用任何 emoji 表情符号，例如 😊 ❤️ ✅ 👋 等
2. 【禁止】使用 Markdown 格式，例如 **加粗**、# 标题、- 列表项、`代码` 等
3. 【禁止】使用括号动作描述，例如（微笑）（点头）（叹气）
4. 【禁止】使用省略号"……"或破折号"——"，改用自然停顿的口语表达
5. 回复要简洁自然，像正常说话一样，不要用书面列举格式
6. 如果需要列举内容，用"第一……第二……还有……"等口语方式表达
7. 数字和单位尽量用中文说，例如"三百米"而不是"300m"

【断开连接指令】
当用户明确表达想结束对话、让你离开、不需要你了（例如"退下吧""再见""拜拜"
"关掉""不用了""走吧""滚""好了不说了"等），你需要：
- 先说一句自然的告别语
- 然后在回复的最末尾紧跟输出：[DISCONNECT]
- 示例：好的，有需要随时叫我。[DISCONNECT]
- 注意只在真正告别时才输出 [DISCONNECT]，普通对话不要输出

你叫小智，是一个友善、简洁的语音助手，请始终用口语化的中文回答。"""
