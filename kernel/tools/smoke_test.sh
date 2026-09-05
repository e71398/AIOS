#!/bin/bash
# AIOS v4.0 冒烟测试 — 5分钟快速验证
echo "============================================"
echo "  AIOS v4.0 冒烟测试"
echo "  $(date)"
echo "============================================"
PASS=0; FAIL=0
check(){ if eval "$1" &>/dev/null; then PASS=$((PASS+1)); echo "  ✅ $2"; else FAIL=$((FAIL+1)); echo "  ❌ $2"; fi; }

echo ""
echo "[1] 基础设施"
check "redis-cli ping" "Redis连接"
check "curl -s --max-time 3 http://localhost:3000/api/health | grep -q 'database.*ok'" "Grafana Dashboard在线"

echo ""
echo "[2] 5 Agent状态"
for s in hermes openclaw opencode claude codex; do
    check "python3 ${AIOS_HOME}/kernel/tools/aios_bus.py heartbeat $s 2>/dev/null" "$s 心跳"
done

echo ""
echo "[3] Agent Mesh"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_agent_mesh import init_mesh;init_mesh()\" 2>/dev/null" "Agent Mesh初始化"
check "python3 ${AIOS_HOME}/kernel/tools/aios_agent_supervisor.py --once 2>/dev/null | grep -q scanned" "Supervisor扫描"

echo ""
echo "[4] Event Bus"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_bus import publish_event;publish_event('task.created',{'test':1},'smoke')\" 2>/dev/null" "Event发布"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_bus import get_event_log;e=get_event_log(hours=1,limit=5);assert len(e)>0\" 2>/dev/null" "Event日志"

echo ""
echo "[5] 任务队列"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_priority_queue import enqueue,dequeue,queue_size;e=enqueue('smoke_test','P0');d=dequeue();assert d\" 2>/dev/null" "优先级队列"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_bus import get_queue_status;get_queue_status()\" 2>/dev/null" "总线队列状态"

echo ""
echo "[6] 自探索"
check "python3 ${AIOS_HOME}/kernel/tools/aios_self_diagnose.py 2>/dev/null | grep -q 发现" "自诊断运行"

echo ""
echo "[7] Token追踪"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_bus import get_token_stats;s=get_token_stats(1);assert len(s)>0\" 2>/dev/null" "Token统计"

echo ""
echo "[8] 快照"
check "ls ${AIOS_HOME}/checkpoint/snapshots/*.tar.gz 2>/dev/null | head -1" "快照文件存在"

echo ""
echo "[9] 防火墙"
check "python3 -c \"import sys;sys.path.insert(0,'${AIOS_HOME}/kernel/tools');from aios_firewall import get_mode;print(get_mode())\" 2>/dev/null" "防火墙运行"

echo ""
echo "============================================"
echo "  通过: $PASS ✅  失败: $FAIL ❌  总计: $((PASS+FAIL))"
[ $FAIL -eq 0 ] && echo "  🎉 冒烟测试通过" || echo "  🚨 有失败项"
echo "============================================"
exit $FAIL
