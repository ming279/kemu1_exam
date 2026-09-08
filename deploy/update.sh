#!/usr/bin/env bash
# =============================================================================
# update.sh - 代码更新后重新部署（拉取最新代码 → 同步依赖 → 重启服务）
# 用法：cd kemu1_exam && sudo bash deploy/update.sh
# =============================================================================
set -e
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[1/3] 拉取最新代码 ..."
cd "$APP_DIR"
git pull

echo "[2/3] 同步 Python 依赖 ..."
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" gunicorn gevent gevent-websocket

echo "[3/3] 重启服务 ..."
systemctl restart kemu1
sleep 3
systemctl --no-pager --lines=5 status kemu1 || true

echo ""
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000/login | grep -q 200; then
  echo "更新完成，服务正常运行"
else
  echo "服务未正常响应，请查看日志：journalctl -u kemu1 -n 50"
fi
