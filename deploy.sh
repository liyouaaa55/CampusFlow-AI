#!/usr/bin/env bash
# ============================================================
# CampusFlow AI —— Ubuntu / 腾讯云 Lighthouse 一键部署脚本
#
# 用法（在服务器上、项目根目录内执行）：
#     bash deploy.sh
#
# 可选：用环境变量覆盖端口
#     CAMPUSFLOW_PORT=8080 bash deploy.sh
#
# 脚本做的事：
#   1. 校验 Python 版本
#   2. 创建虚拟环境并按 requirements-lock.txt 安装依赖（版本与开发环境一致）
#   3. 准备 .env（由模板复制，并提示填写 API Key）
#   4. 注册 systemd 服务（开机自启 + 崩溃自动重启）
#   5. 健康检查冒烟
#   6. 打印公网访问地址与防火墙放行提示
#
# 卸载服务：
#     sudo systemctl disable --now campusflow && sudo rm /etc/systemd/system/campusflow.service
# ============================================================

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CAMPUSFLOW_PORT:-8501}"
SERVICE_NAME="campusflow"
VENV_DIR="$APP_DIR/.venv"

echo "============================================"
echo "  CampusFlow AI 部署"
echo "  项目目录：$APP_DIR"
echo "  服务端口：$PORT"
echo "============================================"
echo

# ---------- 1. 校验 Python ----------
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未找到 python3。"
    echo "       请先执行： sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
    exit 1
fi
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "[1/6] Python 版本：$(python3 -V)"
if [ "$PY_VER" != "3.10" ]; then
    echo "      [提示] 本项目在 Python 3.10 上验证通过，当前为 $PY_VER，如遇依赖问题请改用 3.10。"
fi

# ---------- 2. 虚拟环境与依赖 ----------
if [ ! -d "$VENV_DIR" ]; then
    echo "[2/6] 创建虚拟环境 .venv ..."
    python3 -m venv "$VENV_DIR"
else
    echo "[2/6] 复用已存在的虚拟环境 .venv"
fi

if [ -f "$APP_DIR/requirements-lock.txt" ]; then
    REQ_FILE="$APP_DIR/requirements-lock.txt"
    echo "      使用锁定版本文件 requirements-lock.txt（保证与开发环境一致）"
else
    REQ_FILE="$APP_DIR/requirements.txt"
    echo "      未找到锁定文件，改用 requirements.txt"
fi

echo "      安装依赖中（首次约 2~5 分钟，请耐心等待）..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$REQ_FILE" --quiet
echo "      依赖安装完成"

# ---------- 3. 准备 .env ----------
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "[3/6] 已由 .env.example 创建 .env"
else
    echo "[3/6] 已存在 .env，保持不变"
fi

if grep -qE "sk-x{5,}" "$APP_DIR/.env" 2>/dev/null; then
    echo
    echo "      ⚠️  .env 中仍是占位符，【事项识别】功能将不可用。"
    echo "          请执行以下命令填入真实 API Key："
    echo "              nano $APP_DIR/.env"
    echo "          编辑后重启服务： sudo systemctl restart $SERVICE_NAME"
    echo
fi

# ---------- 4. 注册 systemd 服务 ----------
echo "[4/6] 注册 systemd 服务：$SERVICE_NAME"
sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=CampusFlow AI - 高校基层事务 AI 闭环督办平台
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python -m streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
sudo systemctl restart "$SERVICE_NAME"

# ---------- 5. 健康检查 ----------
echo "[5/6] 等待服务启动并做健康检查..."
OK=0
for i in $(seq 1 20); do
    if curl -fsS --max-time 3 "http://localhost:$PORT/_stcore/health" >/dev/null 2>&1; then
        OK=1
        break
    fi
    sleep 2
done

if [ "$OK" = "1" ]; then
    echo "      健康检查通过：http://localhost:$PORT/_stcore/health"
else
    echo "      [错误] 服务未就绪，最近日志如下："
    sudo journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    echo
    echo "      请排查后重试；本机若不联网也会导致页面报错。"
    exit 1
fi

# ---------- 6. 完成提示 ----------
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<你的公网IP>')"

echo "[6/6] 部署完成"
echo
echo "  访问地址： http://$PUBLIC_IP:$PORT"
echo
echo "  若浏览器打不开，请在腾讯云控制台放行防火墙："
echo "      轻量应用服务器 → 防火墙 → 添加规则 → TCP / $PORT / 0.0.0.0/0"
echo
echo "  常用运维命令："
echo "      查看状态： sudo systemctl status $SERVICE_NAME"
echo "      实时日志： sudo journalctl -u $SERVICE_NAME -f"
echo "      重启服务： sudo systemctl restart $SERVICE_NAME"
echo "      停止服务： sudo systemctl stop $SERVICE_NAME"
echo
