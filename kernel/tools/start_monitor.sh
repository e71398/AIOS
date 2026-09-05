#!/bin/bash
while true; do
    python3 ${AIOS_HOME}/kernel/tools/aios_monitor.py --port 8086
    echo "$(date): monitor crashed, restarting in 3s..." >> /tmp/mon_crash.log
    sleep 3
done
