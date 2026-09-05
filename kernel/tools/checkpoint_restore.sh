#!/bin/bash
# AIOS v4.0 断点恢复脚本
# 用法：./checkpoint_restore.sh <snapshot_name>

AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"

if [ -z "$1" ]; then
    echo "用法: $0 <snapshot_name>"
    echo "可用快照:"
    ls -1 "$AIOS_HOME/checkpoint/snapshots/"*.tar.gz 2>/dev/null | xargs -I{} basename {} .tar.gz || echo "无快照"
    exit 1
fi

SNAPSHOT_NAME="$1"
SNAPSHOT_PATH="$AIOS_HOME/checkpoint/snapshots/${SNAPSHOT_NAME}.tar.gz"
RESTORE_DIR="/tmp/aios_restore_$$"

if [ ! -f "$SNAPSHOT_PATH" ]; then
    echo "❌ 快照不存在: $SNAPSHOT_PATH"
    exit 1
fi

echo "⚠️  警告：恢复将覆盖当前状态"
read -p "确认继续？(y/n): " confirm

if [ "$confirm" != "y" ]; then
    echo "取消恢复"
    exit 0
fi

echo "[$(date)] 开始恢复快照: $SNAPSHOT_NAME"

# 1. 解压快照
echo "  → 解压快照..."
mkdir -p $RESTORE_DIR
tar -xzf $SNAPSHOT_PATH -C $RESTORE_DIR

# 2. 恢复任务上下文
echo "  → 恢复任务上下文..."
SNAPSHOT_CONTENT=$(find $RESTORE_DIR -type d -name "snapshot_*" | head -1)
if [ -n "$SNAPSHOT_CONTENT" ]; then
    # 恢复到对应目录
    cp "$SNAPSHOT_CONTENT"/*.json "$AIOS_HOME/logs/task_logs/" 2>/dev/null
    echo "    任务文件已恢复"
fi

# 3. 恢复 WAL
echo "  → 恢复事务日志..."
WAL_SOURCE=$(find $RESTORE_DIR -type d -name "wal" | head -1)
if [ -n "$WAL_SOURCE" ]; then
    cp -r "$WAL_SOURCE"/* "$AIOS_HOME/checkpoint/wal/" 2>/dev/null
    echo "    WAL 已恢复"
fi

# 4. 清理临时文件
rm -rf $RESTORE_DIR

echo "✅ 恢复完成"
echo "   快照ID: $SNAPSHOT_NAME"
echo "   恢复时间: $(date)"
