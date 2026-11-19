"""State machine rules (spec §4)."""
import pytest

from notesagent.common import states
from notesagent.common.states import PIPELINE_ORDER, TERMINAL, JobStatus as S

FORWARD = [
    (S.QUEUED, S.TRANSCRIBING), (S.TRANSCRIBING, S.SUMMARIZING), (S.SUMMARIZING, S.EXTRACTING_FRAMES),
    (S.EXTRACTING_FRAMES, S.PUBLISHING), (S.PUBLISHING, S.COMPLETED), (S.PUBLISHING, S.COMPLETED_WITH_WARNINGS),
]
NON_TERMINAL = [s for s in S if s not in TERMINAL]


@pytest.mark.parametrize("a,b", FORWARD)
def test_forward_transitions_allowed(a, b):
    assert states.can_transition(a, b)


@pytest.mark.parametrize("s", NON_TERMINAL)
@pytest.mark.parametrize("failure", [S.FAILED, S.DEAD_LETTERED])
def test_any_non_terminal_can_fail_or_dead_letter(s, failure):
    assert states.can_transition(s, failure)


@pytest.mark.parametrize("a,b", [
    (S.QUEUED, S.SUMMARIZING),            # skipping a stage
    (S.QUEUED, S.COMPLETED),
    (S.SUMMARIZING, S.TRANSCRIBING),      # going backwards
    (S.EXTRACTING_FRAMES, S.COMPLETED),   # completing before publishing
    (S.TRANSCRIBING, S.TRANSCRIBING),     # self-transition
    (S.QUEUED, S.QUEUED),
])
def test_illegal_transitions_rejected(a, b):
    assert not states.can_transition(a, b)


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
@pytest.mark.parametrize("target", list(S))
def test_terminal_states_are_final(terminal, target):
    assert not states.can_transition(terminal, target)


def test_pipeline_order_and_next_stage():
    assert PIPELINE_ORDER == (S.TRANSCRIBING, S.SUMMARIZING, S.EXTRACTING_FRAMES, S.PUBLISHING)
    assert states.next_stage(S.QUEUED) is S.TRANSCRIBING
    assert states.next_stage(S.EXTRACTING_FRAMES) is S.PUBLISHING
    assert states.next_stage(S.PUBLISHING) is None


def test_progress_is_monotonic_along_happy_path():
    path = [S.QUEUED, *PIPELINE_ORDER, S.COMPLETED]
    values = [states.progress_for(s) for s in path]
    assert values == sorted(values) and values[0] == 0 and values[-1] == 100
    assert states.progress_for(S.FAILED) is None  # failure keeps last progress
