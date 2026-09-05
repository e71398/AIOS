#!/bin/bash
# AIOS 系统服务安装脚本
# 用法: sudo bash install_services.sh

set -e

SYSTEMD_DIR="/etc/systemd/system"
TOOLS_DIR="${AIOS_HOME}/kernel/tools"

echo "========================================"
echo "  AIOS 系统服务安装"
echo "========================================"

# 1. 复制 service 文件
echo ""
echo "[1/4] 复制 service 文件..."
sudo cp "$TOOLS_DIR/aios-executor-supervisor.service" "$SYSTEMD_DIR/"
sudo cp "$TOOLS_DIR/aios-event-daemon.service" "$SYSTEMD_DIR/"
sudo cp "$TOOLS_DIR/aios-entry-gateway.service" "$SYSTEMD_DIR/"
sudo cp "$TOOLS_DIR/aios.target" "$SYSTEMD_DIR/"
echo "  ✅ 已复制到 $SYSTEMD_DIR"

# 2. 重新加载 systemd
echo ""
echo "[2/4] 重新加载 systemd..."
sudo systemctl daemon-reload
echo "  ✅ 已加载"

# 3. 启用服务（开机自启）
echo ""
echo "[3/4] 启用开机自启..."
sudo systemctl enable aios-executor-supervisor.service
sudo systemctl enable aios-event-daemon.service
sudo systemctl enable aios-entry-gateway.service
sudo systemctl enable aios.target
echo "  ✅ 已启用"

# 4. 启动
echo ""
echo "[4/4] 启动 AIOS 服务..."
sudo systemctl start aios-executor-supervisor.service
sleep 2
sudo systemctl start aios-event-daemon.service
sleep 1
sudo systemctl start aios-entry-gateway.service
echo "  ✅ 已启动"

echo ""
echo "========================================"
echo "  AIOS 服务状态"
echo "========================================"
sudo systemctl status aios-executor-supervisor.service --no-pager 2>&1 | head -10
echo "..."
sudo systemctl status aios-event-daemon.service --no-pager 2>&1 | head -5
echo "..."
sudo systemctl status aios-entry-gateway.service --no-pager 2>&1 | head -5

echo ""
echo "📋 管理命令:"
echo "  sudo systemctl start/stop/restart aios.target       # 全部"
echo "  sudo systemctl start/stop/restart aios-executor-supervisor.service"
echo "  sudo journalctl -u aios-executor-supervisor -f     # 查看日志"
echo ""
echo "✅ AIOS 系统服务安装完成"
