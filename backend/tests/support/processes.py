"""Start real service processes (workers, publishers) and kill them with SIGKILL."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Processes:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, name: str, module: str, *args: object, **env: object) -> subprocess.Popen:
        full_env = os.environ.copy()
        full_env.update({k: str(v) for k, v in env.items()})
        full_env["WORKER_ID"] = name
        log = open(self.log_dir / f"{name}.log", "wb")  # noqa: SIM115 - closed with the process
        proc = subprocess.Popen([sys.executable, "-m", module, *map(str, args)], env=full_env, cwd=BACKEND_ROOT,
                                stdout=log, stderr=subprocess.STDOUT)
        self.procs[name] = proc
        return proc

    def name_of(self, pid: int) -> str:
        return next(n for n, p in self.procs.items() if p.pid == pid)

    def sigkill(self, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)

    def log(self, name: str) -> str:
        path = self.log_dir / f"{name}.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def all_logs(self) -> str:
        return "\n".join(self.log(n) for n in self.procs)

    def wait_for_log(self, name: str, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.log(name):
                return
            if self.procs[name].poll() is not None:
                raise AssertionError(f"{name} exited early:\n{self.log(name)[-3000:]}")
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {text!r} in {name} log:\n{self.log(name)[-3000:]}")

    def stop_all(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
