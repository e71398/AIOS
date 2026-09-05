#!/bin/bash
while true; do
    python3 ${AIOS_HOME}/kernel/tools/aios_executor_supervisor.py
    echo "$(date): executor_supervisor crashed, restarting..." >> ${AIOS_HOME}/logs/supervisor_crash.log
    sleep 3
done
