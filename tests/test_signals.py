#!/usr/bin/env python3
"""Deterministic detail-description signal tests; no LLM or network required."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.signals import score_description  # noqa: E402


def test_multigenerational_words_create_review_signal():
    signal = score_description("Lower-level studio with separate entrance and second kitchen.")
    assert signal.multigenerational is True
    assert "separate entrance" in signal.reasons
    assert "second kitchen" in signal.reasons


def test_busy_road_is_warning_not_deletion_rule():
    signal = score_description("Well-kept home on a busy road near a commercial corridor.")
    assert signal.review_warning is True
    assert signal.reject is False


def test_empty_description_has_no_signals():
    signal = score_description(None)
    assert signal.reasons == ()
    assert signal.multigenerational is False


if __name__ == "__main__":
    test_multigenerational_words_create_review_signal()
    test_busy_road_is_warning_not_deletion_rule()
    test_empty_description_has_no_signals()
    print("PASS — listing description signals are deterministic")
