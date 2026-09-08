#!/usr/bin/env bash
# =============================================================================
# update.sh - 代码更新后重新部署（拉取最新代码 → 同步依赖 → 重启服务）
# 用法：cd kemu1_exam && sudo bash deploy/update.sh
# =============================================================================
set -e
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 以 root 运行时，git/pip 降权为 ubuntu 执行，避免产生 root 属主文件
# （root 属主的 .git/objects 会导致 ubuntu 用户手动 git pull 报 insufficient permission）
if [ "$(id -u)" -eq 0 ]; then
  chown -R ubuntu:ubuntu "$APP_DIR/.git"   # 修复历史残留的 root 属主对象
  RUN="sudo -H -u ubuntu"
else
  RUN=""
fi

echo "[1/3] 拉取最新代码 ..."
cd "$APP_DIR"
$RUN git pull || {
  echo "GitHub 直连失败，改用 gitclone.com 镜像重试 ..."
  $RUN git pull https://gitclone.com/github.com/ming279/kemu1_exam.git main
}

echo "[2/3] 同步 Python 依赖 ..."
$RUN "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" gunicorn gevent gevent-websocket

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
