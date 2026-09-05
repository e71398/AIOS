# AIOS v4.0 终端别名配置
# 添加到 ~/.bashrc: source ${AIOS_HOME}/kernel/tools/aios_aliases.sh

export AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"
export AIOS_ENTRY_GATEWAY="${AIOS_ENTRY_GATEWAY:-http://127.0.0.1:18801}"

# ==================== 核心命令 ====================

# 主命令：发送任务 (短命令)
alias aios='python3 $AIOS_HOME/kernel/tools/aios_entry_gateway.py'

# 入口网关：统一任务提交 (HTTP API)
aios-entry-gateway() {
    if [ -z "$1" ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
        echo "AIOS 统一入口网关"
        echo ""
        echo "服务:"
        echo "  aios-entry-gateway                     启动 HTTP 服务(:18801)"
        echo "  aios-entry-gateway --status            查看系统状态"
        echo "  aios-entry-gateway --health            健康检查"
        echo ""
        echo "提交任务:"
        echo "  aios-entry-gateway \"修复日志错误\"       提交任务"
        echo "  aios-entry-gateway --source=feishu \"任务\"  指定来源"
        echo "  aios-entry-gateway --sender=user \"任务\"    指定发送者"
        echo ""
        echo "HTTP API (服务运行后):"
        echo "  curl http://127.0.0.1:18801/health"
        echo "  curl http://127.0.0.1:18801/status"
        echo "  curl -X POST http://127.0.0.1:18801/task \\"
        echo '       -H "Content-Type: application/json" \\'
        echo '       -d "{\\"input\\":\\"修复错误\\",\\"source\\":\\"cli\\"}"'
        return 0
    fi
    python3 $AIOS_HOME/kernel/tools/aios_entry_gateway.py "$@"
}

# 创建新任务 (使用 HTTP API 或直接调度)
aios-task() {
    if [ -z "$1" ]; then
        echo "用法: aios-task \"任务描述\""
        echo "       aios-task --source=feishu \"任务\""
        echo "       aios-task --http \"任务\"    (走 HTTP 网关)"
        return 1
    fi

    # 检查是否走 HTTP 网关
    for arg in "$@"; do
        if [ "$arg" = "--http" ]; then
            # 过滤掉 --http 参数
            local clean_args=()
            for a in "$@"; do
                [ "$a" != "--http" ] && clean_args+=("$a")
            done
            # 通过 HTTP API 提交
            local payload=$(printf '%s' "${clean_args[*]}" | python3 -c "
import sys, json
input_text = sys.stdin.read().strip()
if '--source=' in input_text or '--sender=' in input_text:
    # Strip flags for input
    parts = input_text.split()
    source = 'cli'
    sender = 'local'
    clean = []
    for p in parts:
        if p.startswith('--source='): source = p.split('=',1)[1]
        elif p.startswith('--sender='): sender = p.split('=',1)[1]
        else: clean.append(p)
    input_text = ' '.join(clean)
    print(json.dumps({'input': input_text, 'source': source, 'sender': sender}))
else:
    print(json.dumps({'input': input_text, 'source': 'cli', 'sender': 'local'}))
")
            curl -s -X POST "$AIOS_ENTRY_GATEWAY/task" \
                -H "Content-Type: application/json" \
                -d "$payload" | python3 -m json.tool 2>/dev/null || \
            echo "❌ 网关未运行 (尝试直接调度)"
            return $?
        fi
    done

    # 默认直接调度 (无需 HTTP 服务)
    python3 $AIOS_HOME/kernel/tools/aios_entry_gateway.py "$@"
}

# ==================== 状态 & 健康 ====================

# 系统状态查看
aios-status() {
    # 优先走 HTTP 网关
    if curl -sf "$AIOS_ENTRY_GATEWAY/status" > /dev/null 2>&1; then
        curl -s "$AIOS_ENTRY_GATEWAY/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('=' * 50)
print('  AIOS 系统状态 (via Entry Gateway)')
print('=' * 50)
q = d.get('queue', {})
print(f'\n  📊 任务队列:  {q.get(\"pending\",0)}待处理 / {q.get(\"running\",0)}运行中 / {q.get(\"completed\",0)}完成 / {q.get(\"failed\",0)}失败')
ex = d.get('executors', {})
print(f'  🤖 执行器:    {ex.get(\"count\",0)} 个已注册')
for e in ex.get('list', [])[:3]:
    print(f'    • {e.get(\"name\",\"?\")} {\"🎯\" if e.get(\"is_orchestrator\") else \"\"}')
svc = d.get('services', {})
print(f'  🏗️  服务:')
for name, status in svc.items():
    icon = '✅' if status == 'running' else ('❌' if status == 'down' else '❓')
    print(f'    {icon} {name}')
tasks = d.get('recent_tasks', [])
if tasks:
    print(f'  📋 最近任务:')
    for t in tasks[:3]:
        icon = {'completed':'✅','failed':'❌','pending':'⏳','running':'🔄'}.get(t.get('status',''),'❓')
        print(f'    {icon} {t.get(\"task_id\",\"?\")[:12]}... → {t.get(\"status\",\"?\")}')
" 2>/dev/null && return 0
    fi

    # 降级到本地检查
    echo "=== AIOS 系统状态 (本地) ==="
    echo "AIOS_HOME: $AIOS_HOME"
    echo ""
    echo "=== 核心文件 ==="
    for f in capability_protocol.json safety_boundary.md context_bus.yaml; do
        if [ -f "$AIOS_HOME/kernel/protocols/$f" ]; then
            echo "  ✅ $f"
        else
            echo "  ❌ $f"
        fi
    done
    echo ""
    echo -n "Redis: "
    redis-cli ping 2>/dev/null && echo "  ✅ 运行中" || echo "  ❌ 未运行"
    echo ""
    echo -n "Entry Gateway: "
    curl -sf "$AIOS_ENTRY_GATEWAY/health" > /dev/null 2>&1 && echo "✅" || echo "❌"
}

# 健康检查
aios-health() {
    echo "=== AIOS 健康检查 ==="
    echo -n "Python3: "
    python3 --version 2>/dev/null || echo "❌"

    echo -n "Redis: "
    redis-cli ping 2>/dev/null && echo "✅" || echo "❌"

    echo -n "Entry Gateway (:18801): "
    curl -sf "$AIOS_ENTRY_GATEWAY/health" > /dev/null 2>&1 && echo "✅" || echo "❌"

    echo -n "Model Gateway (:9998): "
    curl -sf http://127.0.0.1:9998/ > /dev/null 2>&1 && echo "✅" || echo "❌"

    echo -n "Control Center (:8080): "
    curl -sf http://127.0.0.1:8080/ > /dev/null 2>&1 && echo "✅" || echo "❌"

    echo -n "AIOS_HOME: "
    [ -d "$AIOS_HOME" ] && echo "✅ $AIOS_HOME" || echo "❌"

    echo -n "核心协议: "
    count=$(ls $AIOS_HOME/kernel/protocols/*.json $AIOS_HOME/kernel/protocols/*.md $AIOS_HOME/kernel/protocols/*.yaml 2>/dev/null | wc -l)
    echo "$count 个文件"

    echo -n "工具脚本: "
    count=$(ls $AIOS_HOME/kernel/tools/*.py 2>/dev/null | wc -l)
    echo "$count 个脚本"
}

# ==================== 实用工具 ====================

# 查看最近日志
aios-logs() {
    echo "=== 最近任务日志 ==="
    ls -lt $AIOS_HOME/logs/task_logs/ 2>/dev/null | head -10 || echo "  无任务记录"
    echo ""
    echo "=== 最近验证报告 ==="
    for f in $(ls -t $AIOS_HOME/logs/verification_reports/*.json 2>/dev/null | head -3); do
        echo "--- $f ---"
        python3 -m json.tool "$f" 2>/dev/null | head -10
    done
}

# 验证报告查看
aios-verify-report() {
    latest=$(ls -t $AIOS_HOME/logs/verification_reports/*.json 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
        python3 -m json.tool "$latest"
    else
        echo "无验证报告"
    fi
}

# 运行系统测试
aios-test() {
    python3 $AIOS_HOME/kernel/tools/aios_test.py
}

# 运行验证器
aios-verify() {
    if [ -z "$1" ]; then
        echo "用法: aios-verify <task.json>"
        return 1
    fi
    python3 $AIOS_HOME/kernel/tools/verify.py "$1"
}

# 世界模型模拟
aios-simulate() {
    if [ -z "$1" ]; then
        echo "用法: aios-simulate <task.json>"
        return 1
    fi
    python3 $AIOS_HOME/kernel/tools/world_model_runner.py "$1"
}

# 系统快照
aios-snapshot() {
    bash $AIOS_HOME/kernel/tools/checkpoint_snapshot.sh
}

echo "AIOS v4.0 别名已加载"
echo "  命令: aios, aios-task, aios-entry-gateway, aios-status, aios-health"
echo "  工具: aios-logs, aios-verify, aios-test, aios-simulate, aios-snapshot"
