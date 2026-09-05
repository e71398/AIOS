#!/bin/bash
# AIOS v4.0 一键部署脚本
# 用法: bash install.sh

set -e

AIOS_HOME="${AIOS_HOME:-${AIOS_HOME}}"

echo "========================================================"
echo "  AIOS v4.0 一键部署脚本"
echo "  2026-07-05"
echo "  AIOS_HOME: $AIOS_HOME"
echo "========================================================"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 未安装，请先安装 Python 3.11+"
    exit 1
fi
echo "✅ Python3: $(python3 --version)"

# 检查 Redis
REDIS_AVAILABLE=false
if command -v redis-cli &> /dev/null; then
    if redis-cli ping &> /dev/null; then
        echo "✅ Redis: 运行中"
        REDIS_AVAILABLE=true
    else
        echo "⚠️  Redis CLI 可用但服务未运行"
    fi
else
    echo "⚠️  Redis 未安装（将使用文件系统模式）"
fi

# 创建目录
echo ""
echo "[1/5] 创建目录结构..."
bash -c "
mkdir -p $AIOS_HOME/{kernel/{protocols,prompts,tools,config},agents/{evolution/hermes,dispatcher/openclaw,executors/{claude_code,opencode,codex},tools/vscode},knowledge/{industry_specs,skill_library,decision_logs,memory_archive,pending_approval},sandbox/{coding,plc,docs,testing},logs/{task_logs,error_logs,audit_logs,verification_reports,world_model_reports},checkpoint/{snapshots,wal},cache/{vector_db,redis_dump},docs}
chmod -R 755 $AIOS_HOME
chmod 700 $AIOS_HOME/checkpoint
"
echo "✅ 目录结构创建完成"

# 设置脚本权限
echo "[2/5] 设置脚本执行权限..."
chmod +x $AIOS_HOME/kernel/tools/*.py 2>/dev/null || true
chmod +x $AIOS_HOME/kernel/tools/*.sh 2>/dev/null || true
chmod +x $AIOS_HOME/*.sh 2>/dev/null || true
echo "✅ 脚本权限设置完成"

# 运行 VFS 权限隔离
echo "[3/5] 配置 VFS 安全隔离..."
bash $AIOS_HOME/kernel/tools/setup_vfs.sh 2>/dev/null || echo "  VFS 跳过（非 root）"

# 验证核心文件
echo "[4/5] 验证核心文件..."
MISSING=0
check_file() {
    if [ -f "$1" ]; then
        echo "  ✅ $2"
    else
        echo "  ❌ $2 缺失: $1"
        MISSING=$((MISSING + 1))
    fi
}

check_file "$AIOS_HOME/kernel/protocols/capability_protocol.json" "能力协议"
check_file "$AIOS_HOME/kernel/protocols/safety_boundary.md"       "安全边界"
check_file "$AIOS_HOME/kernel/protocols/context_bus.yaml"         "信息总线"
check_file "$AIOS_HOME/kernel/tools/verify.py"                    "验证脚本"
check_file "$AIOS_HOME/kernel/tools/aios_quick.py"                "快捷入口"
check_file "$AIOS_HOME/kernel/tools/world_model_runner.py"        "世界模型"
check_file "$AIOS_HOME/kernel/config/rbac_policy.json"            "RBAC策略"
check_file "$AIOS_HOME/docker-compose.yml"                        "Docker编排"

# 运行测试
echo "[5/5] 运行系统测试..."
echo ""
python3 $AIOS_HOME/kernel/tools/aios_test.py

echo ""
echo "========================================================"
echo "  部署完成！"
echo ""
echo "  下一步操作："
echo "  1. 添加终端别名到 ~/.bashrc:"
echo "     source $AIOS_HOME/kernel/tools/aios_aliases.sh"
echo "  2. 测试快捷命令:"
echo "     python3 $AIOS_HOME/kernel/tools/aios_quick.py \"测试任务\""
echo "  3. 查看系统状态: aios-status"
echo "========================================================"
