#!/bin/bash
# AIOS v4.0 VFS 虚拟文件系统权限隔离配置
# 实现 Ring 0-3 权限分级

AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"

echo "=== AIOS VFS 权限隔离配置 ==="
echo "AIOS_HOME: $AIOS_HOME"

# Ring 0: 核心资产（只读，任何 Agent 禁止写入）
echo "[Ring 0] 配置核心知识库只读权限..."
chmod 555 "$AIOS_HOME/kernel"
chmod 444 "$AIOS_HOME/kernel/protocols"/*.json 2>/dev/null
chmod 444 "$AIOS_HOME/kernel/protocols"/*.md 2>/dev/null
chmod 444 "$AIOS_HOME/kernel/protocols"/*.yaml 2>/dev/null
chmod 555 "$AIOS_HOME/knowledge/industry_specs" 2>/dev/null
echo "  Ring 0 完成: 核心协议只读"

# Ring 1: 沙盒验证区（可读写，但隔离）
echo "[Ring 1] 配置沙盒隔离区..."
chmod 755 "$AIOS_HOME/sandbox"
chmod 777 "$AIOS_HOME/sandbox/coding"
chmod 700 "$AIOS_HOME/sandbox/plc"
echo "  Ring 1 完成: 沙盒可读写"

# Ring 2: 经验更新区（Hermes + 人类双重签名才可写）
echo "[Ring 2] 配置经验更新区..."
chmod 730 "$AIOS_HOME/knowledge/pending_approval" 2>/dev/null
echo "  Ring 2 完成: 经验区受限写入"

# Ring 3: 日志与临时区
echo "[Ring 3] 配置日志区..."
chmod 755 "$AIOS_HOME/logs"
chmod 700 "$AIOS_HOME/logs/error_logs"
echo "  Ring 3 完成: 日志区可写入"

echo ""
echo "=== 权限验证 ==="
echo "kernel:     $(stat -c '%a' "$AIOS_HOME/kernel" 2>/dev/null || echo 'N/A')"
echo "knowledge:  $(stat -c '%a' "$AIOS_HOME/knowledge" 2>/dev/null || echo 'N/A')"
echo "sandbox:    $(stat -c '%a' "$AIOS_HOME/sandbox" 2>/dev/null || echo 'N/A')"
echo "logs:       $(stat -c '%a' "$AIOS_HOME/logs" 2>/dev/null || echo 'N/A')"
echo ""
echo "✅ VFS 权限配置完成"
