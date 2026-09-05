#!/bin/bash
# AIOS v4.0 系统快照脚本
# 功能：保存当前系统状态，支持断点恢复

AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"
SNAPSHOT_DIR="$AIOS_HOME/checkpoint/snapshots"
WAL_DIR="$AIOS_HOME/checkpoint/wal"
MAX_SNAPSHOTS=10
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SNAPSHOT_NAME="snapshot_${TIMESTAMP}"
RETENTION_DAYS=30

echo "[$(date)] AIOS 快照开始..."

# 创建快照目录
mkdir -p "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}"

# 1. 保存任务队列状态（从 Redis）
echo "  → 保存任务队列状态..."
redis-cli SAVE 2>/dev/null || echo "    Redis 不可用，跳过"

# 2. 保存当前活跃任务上下文
echo "  → 保存活跃任务上下文..."
ls "$AIOS_HOME/logs/task_logs/"*.json 2>/dev/null | head -50 | while read f; do
    cp "$f" "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}/"
done 2>/dev/null

# 3. 保存 WAL 日志
echo "  → 保存事务日志..."
cp -r "$WAL_DIR"/* "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}/wal/" 2>/dev/null || mkdir -p "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}/wal"

# 4. 生成快照元数据
cat > "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}/metadata.json" << EOF
{
  "snapshot_id": "${SNAPSHOT_NAME}",
  "timestamp": "$(date -Iseconds)",
  "system_version": "AIOS_v4.0",
  "aios_home": "$AIOS_HOME"
}
EOF

# 5. 压缩快照
echo "  → 压缩快照..."
tar -czf "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}.tar.gz" -C "${SNAPSHOT_DIR}" "${SNAPSHOT_NAME}/"
rm -rf "${SNAPSHOT_DIR}/${SNAPSHOT_NAME}"

# 6. 清理旧快照（保留最近 MAX_SNAPSHOTS 个）
echo "  → 清理旧快照..."
cd "${SNAPSHOT_DIR}"
ls -t *.tar.gz 2>/dev/null | tail -n +$((MAX_SNAPSHOTS + 1)) | xargs -r rm -f

# 7. 清理过期快照
find "${SNAPSHOT_DIR}" -name "*.tar.gz" -mtime +${RETENTION_DAYS} -delete 2>/dev/null

echo "[$(date)] AIOS 快照完成: ${SNAPSHOT_NAME}"
echo "快照文件: ${SNAPSHOT_DIR}/${SNAPSHOT_NAME}.tar.gz"
