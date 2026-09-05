#!/bin/bash
# AIOS Task Console 启动脚本
cd ${AIOS_HOME}/kernel/tools
nohup python3 aios_web.py > /tmp/aios_task_console.log 2>&1 &
echo "PID=$!"
echo "🎯 http://localhost:8080"
echo "日志: /tmp/aios_task_console.log"
