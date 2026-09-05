#!/bin/bash
# AIOS OpenCode 总线包装器
# 用法: aios-opencode "你的任务描述"
# 替代直接使用 opencode 命令，自动在任务完成后写入 Redis 共享总线

TASK_NAME="${1:-未命名任务}"
TIMESTAMP=$(date -Iseconds)
TASK_ID="oc-$(date +%s)-$$"

echo "🔗 AIOS Bus → OpenCode 执行: $TASK_NAME"
echo "   Task ID: $TASK_ID"
echo ""

START_TS=$(date -Iseconds)
START_EPOCH=$(date +%s)

# 执行前: 检查冲突
python3 ${AIOS_HOME}/kernel/tools/aios_bus.py conflict "$TASK_NAME" 2>/dev/null

# 发送心跳
python3 ${AIOS_HOME}/kernel/tools/aios_bus.py heartbeat opencode 2>/dev/null

# 执行 OpenCode
opencode "$@"
EXIT_CODE=$?

END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))
END_TS=$(date -Iseconds)

# 执行后: 发布结果到总线
if [ $EXIT_CODE -eq 0 ]; then
    STATUS="completed"
    SUMMARY="OpenCode 执行成功 (耗时${DURATION}s)"
else
    STATUS="failed"
    SUMMARY="OpenCode 执行失败, exit=$EXIT_CODE (耗时${DURATION}s)"
fi

python3 ${AIOS_HOME}/kernel/tools/aios_bus.py publish \
    --system opencode \
    --task-id "$TASK_ID" \
    --name "$TASK_NAME" \
    --status "$STATUS" \
    --summary "$SUMMARY" \
    --source cli \
    --priority 2 \
    2>/dev/null

echo ""
echo "🔗 结果已写入共享总线 [$STATUS]"

exit $EXIT_CODE
