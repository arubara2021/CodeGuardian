"""Tests for the per-phase BudgetTracker.

The tracker enforces two independent caps: number of tool calls and
total output bytes. When either cap is exceeded, ``check_call`` or
``record_output`` raises ``PLUGIN_TIMEOUT`` with a ``details["budget"]``
field naming which cap fired. The phase catches this as a phase-level
stop, not a per-call retry.
"""

from __future__ import annotations

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker


def test_no_caps_means_no_error():
    tracker = BudgetTracker(max_calls=None, max_output_bytes=None)
    for _ in range(1000):
        tracker.check_call()
        tracker.record_output(100)
    assert tracker.calls == 1000
    assert tracker.output_bytes == 100_000


def test_call_cap_triggers_on_exceed():
    tracker = BudgetTracker(max_calls=3, max_output_bytes=None)
    for _ in range(3):
        tracker.check_call()
    assert tracker.calls == 3
    with pytest.raises(CodeGuardianError) as exc_info:
        tracker.check_call()
    assert exc_info.value.code is ErrorCode.PLUGIN_TIMEOUT
    assert exc_info.value.details["budget"] == "calls"
    assert exc_info.value.details["current"] == 4
    assert exc_info.value.details["limit"] == 3


def test_byte_cap_triggers_on_exceed():
    tracker = BudgetTracker(max_calls=None, max_output_bytes=100)
    tracker.record_output(60)
    tracker.record_output(40)
    assert tracker.output_bytes == 100
    with pytest.raises(CodeGuardianError) as exc_info:
        tracker.record_output(1)
    assert exc_info.value.code is ErrorCode.PLUGIN_TIMEOUT
    assert exc_info.value.details["budget"] == "output_bytes"
    assert exc_info.value.details["current"] == 101
    assert exc_info.value.details["limit"] == 100


def test_call_cap_and_byte_cap_are_independent():
    call_tracker = BudgetTracker(max_calls=2, max_output_bytes=None)
    call_tracker.check_call()
    call_tracker.check_call()
    with pytest.raises(CodeGuardianError) as exc_info:
        call_tracker.check_call()
    assert exc_info.value.details["budget"] == "calls"

    byte_tracker = BudgetTracker(max_calls=None, max_output_bytes=10)
    byte_tracker.record_output(10)
    with pytest.raises(CodeGuardianError) as exc_info2:
        byte_tracker.record_output(1)
    assert exc_info2.value.details["budget"] == "output_bytes"


def test_zero_cap_triggers_immediately():
    tracker = BudgetTracker(max_calls=0, max_output_bytes=None)
    with pytest.raises(CodeGuardianError):
        tracker.check_call()


def test_counters_accumulate():
    tracker = BudgetTracker(max_calls=10, max_output_bytes=1000)
    for _ in range(5):
        tracker.check_call()
        tracker.record_output(100)
    assert tracker.calls == 5
    assert tracker.output_bytes == 500


def test_exact_cap_value_is_allowed():
    tracker = BudgetTracker(max_calls=3, max_output_bytes=30)
    for _ in range(3):
        tracker.check_call()
        tracker.record_output(10)
    assert tracker.calls == 3
    assert tracker.output_bytes == 30
    with pytest.raises(CodeGuardianError):
        tracker.check_call()
