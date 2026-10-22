#!/usr/bin/env bash
# 在 Ubuntu / Debian 云服务器上一键部署（22.04 / 24.04 / 26.04 通吃）
# 用法：把 dream-monitor-slim.zip 传到服务器 /root 后
#   cd /root && unzip -o dream-monitor-slim.zip -d /root/dream-monitor
#   sudo bash /root/dream-monitor/deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
PORT="${DM_PORT:-8770}"
CODE="${DM_CODE:-FuXiaoSheng}"

echo "==> 目录：$APP_DIR"
echo "==> 用户：$SERVICE_USER · 端口：$PORT · 口令：$CODE"
echo "==> 系统：$(. /etc/os-release && echo "$PRETTY_NAME")"

# ---------------------------------------------------------------- 1. Python
if ! command -v python3 >/dev/null; then
  apt-get update -y && apt-get install -y python3 python3-pip
fi
python3 --version

# ---------------------------------------------------------------- 2. PyYAML
# 顺序：apt 系统包（免编译，26.04 也能用）→ venv → 强行 pip
YAML_OK=0
if python3 -c "import yaml" 2>/dev/null; then
  echo "==> PyYAML 已就绪"
  YAML_OK=1
else
  echo "==> 尝试 apt 安装 python3-yaml"
  apt-get update -y || true
  if apt-get install -y python3-yaml 2>/dev/null && python3 -c "import yaml" 2>/dev/null; then
    YAML_OK=1
    echo "==> 通过 apt 装好 PyYAML"
  fi
fi

if [ "$YAML_OK" -ne 1 ]; then
  echo "==> apt 不行，改用 venv"
  apt-get install -y python3-venv python3-pip || true
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
  "$APP_DIR/.venv/bin/pip" install pyyaml==6.0.2
  if ! "$APP_DIR/.venv/bin/python" -c "import yaml" 2>/dev/null; then
    echo "!! venv 也失败了，最后尝试 --break-system-packages"
    python3 -m pip install --break-system-packages pyyaml==6.0.2
  fi
fi

# 选解释器：venv 存在且能用就用它
PYBIN="/usr/bin/python3"
if [ -x "$APP_DIR/.venv/bin/python" ] && "$APP_DIR/.venv/bin/python" -c "import yaml" 2>/dev/null; then
  PYBIN="$APP_DIR/.venv/bin/python"
fi
echo "==> 使用解释器：$PYBIN"
"$PYBIN" -c "import yaml, sqlite3, urllib.request, http.server; print('依赖自检通过')"

# ---------------------------------------------------------------- 3. 目录权限
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR" 2>/dev/null || true

# ---------------------------------------------------------------- 4. systemd
cat > /etc/systemd/system/dream-monitor.service <<EOF
[Unit]
Description=Dream Space activity monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=DM_HOST=0.0.0.0
Environment=DM_PORT=$PORT
Environment=DM_CODE=$CODE
Environment=DM_AUTO=1
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=$PYBIN $APP_DIR/gui_server.py --no-open
Restart=always
RestartSec=5
# 兜底：默认 1024 个文件描述符，长时间运行容易被句柄耗尽
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dream-monitor
sleep 3
systemctl --no-pager --lines=8 status dream-monitor || true

IP="$(curl -s --max-time 6 https://api.ipify.org || echo 你的服务器IP)"
echo
echo "================ 部署完成 ================"
echo "  打开：http://$IP:$PORT/?k=$CODE"
echo "  记得在腾讯云控制台「防火墙」放行 TCP $PORT"
echo "  看日志：journalctl -u dream-monitor -f"
echo "  重启  ：systemctl restart dream-monitor"
echo "=========================================="
