#!/bin/bash
while true; do
    python3 ${AIOS_HOME}/kernel/tools/aios_entry_gateway.py
    echo "$(date): entry_gateway crashed, restarting..." >> ${AIOS_HOME}/logs/gateway_crash.log
    sleep 3
done
