#!/bin/bash
while true; do
    python3 -c "
import sys; sys.path.insert(0,'${AIOS_HOME}/kernel/tools')
from aios_runtime_server import render
open('/tmp/runtime_live.html','w').write(render())
" 2>/dev/null
    sleep 10
done
