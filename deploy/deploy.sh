#!/usr/bin/env bash
# =============================================================================
# deploy.sh - 驾照科目一题库系统 云服务器一键部署脚本（Ubuntu 22.04/24.04）
#
# 用法（在服务器上）：
#   git clone https://github.com/ming279/kemu1_exam.git
#   cd kemu1_exam
#   sudo bash deploy/deploy.sh
#
# 脚本完成：安装 MySQL8 + Python 虚拟环境 → 导入题库备份（含 2308 题/图片）
#           → Nginx 反代（80 端口 + WebSocket 条件转发）
#           → gunicorn + GeventWebSocketWorker（支持 WebSocket 双人 PK）
#           → systemd 托管（开机自启、崩溃重启）→ 健康检查
# =============================================================================
set -e

# ---- 0. 基本检查 ----
if [ "$(id -u)" != "0" ]; then
  echo "请用 root 运行：sudo bash deploy/deploy.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="kemu1_exam"
DB_USER="kemu1"
DB_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 20)"
SECRET_KEY="$(head -c 48 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 40)"
ENV_FILE="/etc/kemu1.env"
SERVICE_FILE="/etc/systemd/system/kemu1.service"

echo "=============================================="
echo " 项目目录 : $APP_DIR"
echo " 数据库名 : $DB_NAME"
echo "=============================================="

# ---- 1. 安装系统依赖 ----
echo "[1/8] 安装 MySQL / Python / git / Nginx ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mysql-server python3-pip python3-venv git curl nginx >/dev/null

systemctl enable --now mysql >/dev/null 2>&1 || true

# 等待 MySQL 就绪
for i in $(seq 1 30); do
  if mysqladmin ping >/dev/null 2>&1; then break; fi
  sleep 1
done

# 调大 max_allowed_packet（备份含图片 BLOB，单条 INSERT 可能较大）
echo "[2/8] 配置 MySQL 数据包大小 ..."
cat > /etc/mysql/mysql.conf.d/kemu1.cnf <<'EOF'
[mysqld]
max_allowed_packet=512M
EOF
systemctl restart mysql
sleep 2

# ---- 2. 导入题库备份（备份自带 CREATE DATABASE / USE，hex-blob 格式防二进制损坏）----
echo "[3/8] 导入题库备份 sql/kemu1_exam_backup.sql.gz ..."
if [ ! -f "$APP_DIR/sql/kemu1_exam_backup.sql.gz" ]; then
  echo "错误：找不到 $APP_DIR/sql/kemu1_exam_backup.sql.gz"
  exit 1
fi
gunzip -c "$APP_DIR/sql/kemu1_exam_backup.sql.gz" | mysql --max-allowed-packet=512M --default-character-set=utf8mb4

# ---- 3. 创建专用数据库账号 ----
echo "[4/8] 创建数据库账号 ..."
mysql <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

# ---- 4. Python 虚拟环境与依赖 ----
echo "[5/8] 创建 Python 虚拟环境并安装依赖 ..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
  gunicorn gevent gevent-websocket simple-websocket

# ---- 5. 环境变量文件 ----
echo "[6/8] 写入环境配置 $ENV_FILE ..."
cat > "$ENV_FILE" <<EOF
DB_HOST=127.0.0.1
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASS}
DB_NAME=${DB_NAME}
SECRET_KEY=${SECRET_KEY}
FLASK_DEBUG=0
PORT=5000
EOF
chmod 600 "$ENV_FILE"

# ---- 6. systemd 服务（gunicorn + GeventWebSocketWorker，支持 WebSocket）----
echo "[7/8] 注册 systemd 服务 ..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Kemu1 Exam (Flask + SocketIO)
After=mysql.service network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}/app
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/venv/bin/gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 \
          --bind 0.0.0.0:5000 --timeout 120 --access-logfile - main:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kemu1 >/dev/null 2>&1
systemctl restart kemu1
sleep 4

# ---- 7. Nginx 反代（80 端口 + WebSocket 条件转发）----
echo "[8/8] 配置 Nginx 反代 ..."
cat > /etc/nginx/sites-available/kemu1 <<'NGINX'
# 条件转发：只有请求真的带了 Upgrade 头才发 Connection: upgrade
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 20m;
    proxy_buffering off;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/kemu1 /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null 2>&1 && systemctl restart nginx || echo "⚠ Nginx 配置有误，请检查"

# ---- 8. 防火墙与健康检查 ----
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null 2>&1 || true
  echo "已放行防火墙 80/tcp"
fi

echo ""
echo "=============================================="
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/login | grep -q 200; then
  echo "✅ 部署成功！服务运行中"
else
  echo "⚠ 服务可能还在启动，请稍后检查：systemctl status kemu1"
fi
PUBLIC_IP="$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo '服务器公网IP')"
echo " 🌐 访问地址：http://${PUBLIC_IP}  （Nginx 反代到 gunicorn:5000）"
echo " 👤 管理员账号：admin / admin123"
echo ""
echo " 🔧 常用命令："
echo "   systemctl status kemu1      # 查看后端状态"
echo "   systemctl restart kemu1     # 重启后端"
echo "   journalctl -u kemu1 -f      # 查看后端实时日志"
echo "   systemctl restart nginx     # 重启 Nginx"
echo "   nginx -t                    # 测试 Nginx 配置"
echo " 📄 数据库配置已保存到：${ENV_FILE}"
echo "=============================================="
