#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────
#  xiaozhi-openclaw 一键安装脚本
# ────────────────────────────────────────────────────────────
set -e

echo "[1/4] 检查系统依赖..."
if command -v apt-get &> /dev/null; then
    if ! dpkg -l | grep -q libopus0; then
        echo "    安装 libopus..."
        sudo apt-get update
        sudo apt-get install -y libopus0 libopus-dev ffmpeg
    else
        echo "    libopus 已安装 ✓"
    fi
elif command -v yum &> /dev/null; then
    sudo yum install -y opus opus-devel ffmpeg
elif command -v brew &> /dev/null; then
    brew install opus ffmpeg
else
    echo "    ⚠️  不识别的系统，请手动安装 libopus 和 ffmpeg"
fi

echo "[2/4] 检查 Python 版本..."
if ! command -v python3 &> /dev/null; then
    echo "    ❌ 找不到 python3，请先安装 Python 3.9+"
    exit 1
fi
python3 --version

echo "[3/4] 安装 Python 依赖..."
pip install -r requirements.txt

echo "[4/4] 准备配置文件..."
if [ ! -f config.py ]; then
    cp config.example.py config.py
    echo "    已创建 config.py，请编辑该文件填写 API Key"
else
    echo "    config.py 已存在，跳过 ✓"
fi

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║  ✅ 安装完成                                          ║"
echo "║                                                        ║"
echo "║  下一步：                                              ║"
echo "║    1. 编辑 config.py 填写火山 API Key                 ║"
echo "║    2. 确保 ~/.openclaw/ 下身份文件齐全                ║"
echo "║    3. python server.py                                ║"
echo "╚════════════════════════════════════════════════════════╝"
