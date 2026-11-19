#!/usr/bin/env python3
"""Host wrapper: run the drain-safe work-queue migration inside the worker image.

    python scripts/migrate_work_queue.py --check          # report only, change nothing
    python scripts/migrate_work_queue.py                  # migrate; exit 3 if still draining
    python scripts/migrate_work_queue.py --wait 300       # keep waiting up to 5 min for the drain

Safe to re-run at any time. Requires the stack (RabbitMQ) to be running; uses the
credentials from .env via docker compose. See notesagent/ops/work_queue_migration.py.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    command = ["docker", "compose", "run", "--rm", "--no-deps", "worker",
               "python", "-m", "notesagent.ops.work_queue_migration", *sys.argv[1:]]
    sys.exit(subprocess.call(command, cwd=ROOT))
