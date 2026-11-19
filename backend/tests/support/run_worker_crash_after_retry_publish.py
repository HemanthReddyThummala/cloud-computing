"""TEST-ONLY: the real JobWorker, SIGKILLed in the retry path at the worst moment —
AFTER the replacement message (x-processing-attempts) was confirmed by the broker and
BEFORE the original message is acknowledged. Leaves two copies of the job message.
"""
import os
import signal
from pathlib import Path

from notesagent.common.broker import BrokerClient
from notesagent.worker.__main__ import serve
from tests.support.fixture_stages import build_fixture_stages

_original_republish = BrokerClient.republish


async def _republish_then_die(self, message, *, extra_headers):
    await _original_republish(self, message, extra_headers=extra_headers)   # confirmed by broker
    if "x-processing-attempts" in extra_headers:
        Path(os.environ["FIXTURE_DIR"], "killed-after-retry-publish").write_text("")
        os.kill(os.getpid(), signal.SIGKILL)                                 # ...before message.ack()


BrokerClient.republish = _republish_then_die

if __name__ == "__main__":
    serve(build_fixture_stages())
