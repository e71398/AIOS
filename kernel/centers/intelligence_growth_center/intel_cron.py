"""
Intel Cron — 定时调度 (差异化频率)
GitHub: 每12h / 情报: 每2h / 机会: 每6h / 清理: 每日
"""
import sys, os
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from open_source_radar import scan as scan_opensource
from global_intel_radar import scan as scan_intel
from opportunity_radar import scan as scan_opportunity
from cleanup_engine import CleanupEngine

def run_opensource(): return scan_opensource()
def run_intel(): return scan_intel()
def run_opportunity(): return scan_opportunity()
def run_cleanup(): return CleanupEngine().purge_all_expired()

def run_all():
    return {
        "opensource": run_opensource(),
        "intel": run_intel(),
        "opportunity": run_opportunity(),
        "cleanup": run_cleanup(),
    }

COMMANDS = {
    "opensource": run_opensource,
    "intel": run_intel,
    "opportunity": run_opportunity,
    "cleanup": run_cleanup,
    "all": run_all,
}

if __name__ == "__main__":
    import json
    command = sys.argv[1] if len(sys.argv) > 1 else "all"
    if command not in COMMANDS:
        print(f"unknown command: {command}; choose {','.join(COMMANDS)}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps({command: COMMANDS[command]()}, indent=2,
                     ensure_ascii=False, default=str))
