"""TEST-ONLY entrypoint: the real JobWorker with test fixture stages."""
from notesagent.worker.__main__ import serve
from tests.support.fixture_stages import build_fixture_stages

if __name__ == "__main__":
    serve(build_fixture_stages())
