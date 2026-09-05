#!/bin/bash
while true; do
    python3 ${AIOS_HOME}/kernel/tools/serve_runtime.py 18086
    sleep 2
done
