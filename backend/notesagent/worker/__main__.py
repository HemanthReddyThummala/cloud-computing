"""Run a worker: ``python -m notesagent.worker``."""
from __future__ import annotations

from notesagent.common.config import get_settings
from notesagent.common.logging import setup_logging
from notesagent.common.runtime import run_service
from notesagent.worker.runner import JobWorker
from notesagent.worker.stages import StageRegistry, build_stage_registry


def serve(stages: StageRegistry) -> None:
    settings = get_settings(service_name="worker")
    setup_logging("worker", settings.log_level)
    run_service(JobWorker(settings, stages).run)


def main() -> None:
    serve(build_stage_registry())


if __name__ == "__main__":
    main()
