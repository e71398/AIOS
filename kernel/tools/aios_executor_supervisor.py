#!/usr/bin/env python3
"""
AIOS v4.0 执行器监管进程 (Executor Supervisor)
==============================================
统一管理 opencode / claude / codex 三个执行器的持久运行。
自动重启崩溃的子进程，收集日志，响应停止信号。

用法:
  python3 aios_executor_supervisor.py              # 启动全部3个执行器
  python3 aios_executor_supervisor.py opencode     # 只启动指定执行器
  python3 aios_executor_supervisor.py --status     # 查看状态
"""
import sys, os, time, signal, subprocess, json, threading
from pathlib import Path
from datetime import datetime, timezone

TOOLS = Path("${AIOS_HOME}/kernel/tools")
sys.path.insert(0, str(TOOLS))

from aios_bus import _is_available, init_registry, heartbeat

# P8A: dynamic tool list. ``ALL_EXECUTORS`` is no longer hard-coded;
# the supervisor reads the registry at startup and reflects whatever
# tools are currently registered with the ``executor`` role. The
# inline fallback preserves the historical three-tool behaviour so
# older supervisor instances (and tests) keep working when the
# registry is unavailable.
try:
    from aios_tool_registry import (
        get_default_registry as _default_tool_registry,
    )
except Exception:  # pragma: no cover - extremely defensive
    _default_tool_registry = None


def _discover_executor_tool_ids():
    if _default_tool_registry is not None:
        try:
            reg = _default_tool_registry()
            ids = sorted(m.tool_id for m in reg.list_by_role("executor"))
            if ids:
                return ids
        except Exception:
            pass
    return ["opencode", "claude", "codex"]


ALL_EXECUTORS = _discover_executor_tool_ids()
DAEMON_SCRIPT = "aios_executor_daemon.py"
MAX_RESTART_DELAY = 60  # 最大重启间隔（秒，指数退避）


class ExecutorProcess:
    """管理单个执行器子进程。"""

    def __init__(self, name: str, tools_dir: Path):
        self.name = name
        self.script = str(tools_dir / DAEMON_SCRIPT)
        self.process: subprocess.Popen | None = None
        self.restart_count = 0
        self.restart_delay = 1  # 指数退避初始值
        self.last_start_ts = 0.0
        self._shutdown = False

    def start(self):
        if self._shutdown:
            return
        # 防止重复: 检查是否已有同名的daemon在运行
        import subprocess as _sp
        try:
            r = _sp.run(["pgrep","-f",f"executor_daemon.py.*{self.name}"], capture_output=True, text=True, timeout=2)
            existing = [p for p in r.stdout.strip().split("\n") if p]
            if existing:
                return True  # 已存在,不重复启动
        except Exception:
            pass
        try:
            self.process = subprocess.Popen(
                ["/usr/bin/python3", self.script, self.name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=str(Path(self.script).parent),
            )
            self.last_start_ts = time.time()
            print(f"  ✅ [{self.name}] 启动 (PID={self.process.pid})")
            return True
        except Exception as e:
            print(f"  ❌ [{self.name}] 启动失败: {e}")
            return False

    def poll_output(self, timeout=0.5):
        """读取子进程输出（非阻塞）。"""
        if not self.process or self.process.poll() is not None:
            return []
        import select
        lines = []
        try:
            poller = select.poll()
            poller.register(self.process.stdout, select.POLLIN)
            events = poller.poll(int(timeout * 1000))
            for fd, _ in events:
                if fd == self.process.stdout.fileno():
                    for _ in range(20):  # 最多读20行
                        line = self.process.stdout.readline()
                        if not line:
                            break
                        lines.append(line.rstrip())
        except Exception:
            pass
        return lines

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def should_restart(self) -> bool:
        if self._shutdown:
            return False
        if self.process is None:
            return True
        if self.process.poll() is not None:
            return True
        return False

    def restart(self):
        """指数退避重启。"""
        if self._shutdown:
            return
        self.restart_count += 1
        if self.restart_count > 1:
            self.restart_delay = min(self.restart_delay * 2, MAX_RESTART_DELAY)
            print(f"  ⏳ [{self.name}] 将在 {self.restart_delay}s 后重启 (第{self.restart_count}次)")
            time.sleep(self.restart_delay)
        else:
            time.sleep(1)
        # 重置退避（如果正常运行超过30秒）
        if time.time() - self.last_start_ts > 30:
            self.restart_delay = 1
        self.start()

    def stop(self):
        """优雅停止子进程。"""
        self._shutdown = True
        if self.process and self.process.poll() is None:
            pid = self.process.pid
            print(f"  🛑 [{self.name}] 停止 PID={pid}")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class Supervisor:
    """执行器监督者：管理所有执行器进程。"""

    def __init__(self):
        self.executors = {
            name: ExecutorProcess(name, TOOLS)
            for name in ALL_EXECUTORS
        }
        self._shutdown = False

    def start_all(self, names: list[str] | None = None):
        """启动指定的执行器（默认全部）。"""
        targets = names or ALL_EXECUTORS
        for name in targets:
            if name in self.executors:
                self.executors[name].start()

    def run_forever(self):
        """主循环：监视子进程 + 读取输出。"""
        print("=" * 55)
        print(f"  AIOS Executor Supervisor")
        print(f"  监管: {', '.join(ALL_EXECUTORS)}")
        print(f"  PID: {os.getpid()}")
        print("=" * 55)

        # 信号处理
        def handle_signal(sig, frame):
            print("\n🛑 Supervisor 收到停止信号")
            self._shutdown = True
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        # 心跳
        try:
            init_registry()
        except Exception:
            pass

        while not self._shutdown:
            for name, proc in self.executors.items():
                # 读取并输出子进程日志
                for line in proc.poll_output():
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"[{ts}][{name}] {line}")

                # 检查是否需要重启
                if proc.should_restart():
                    proc.restart()

            time.sleep(0.5)

        # 清理
        print("\n正在停止所有执行器...")
        for proc in self.executors.values():
            proc.stop()
        print("✅ All executors stopped")

    def status(self) -> dict:
        """返回当前状态。"""
        result = {}
        for name, proc in self.executors.items():
            result[name] = {
                "alive": proc.is_alive(),
                "pid": proc.process.pid if proc.process else None,
                "restarts": proc.restart_count,
                "uptime": round(time.time() - proc.last_start_ts, 1) if proc.last_start_ts else 0,
            }
        return result

    def print_status(self):
        """打印状态到控制台。"""
        print(f"\nAIOS 执行器状态 ({datetime.now().strftime('%H:%M:%S')})")
        print("=" * 40)
        for name, info in self.status().items():
            icon = "🟢" if info["alive"] else "🔴"
            pid = f"PID={info['pid']}" if info["pid"] else "stopped"
            uptime = f"{info['uptime']}s" if info["uptime"] else "-"
            restarts = f"重启{info['restarts']}次" if info['restarts'] else ""
            print(f"  {icon} {name:10s} {pid:12s} {uptime:8s} {restarts}")


def main():
    supervisor = Supervisor()

    if "--status" in sys.argv:
        supervisor.print_status()
        return

    # 过滤启动的执行器
    targets = [a for a in sys.argv[1:] if a in ALL_EXECUTORS]
    if not targets:
        targets = ALL_EXECUTORS

    supervisor.start_all(targets)
    supervisor.run_forever()


if __name__ == "__main__":
    main()
