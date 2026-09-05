#!/bin/bash
while true; do
    python3 ${AIOS_HOME}/kernel/tools/aios_web.py --port 8080
    echo "$(date): web crashed, restarting..." >> /tmp/web_crash.log
    sleep 3
done
