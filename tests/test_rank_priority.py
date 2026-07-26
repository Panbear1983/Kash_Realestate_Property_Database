#!/usr/bin/env python3
"""Deterministic ranking overrides; no LLM call is made."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.rank import apply_property_priority  # noqa: E402


def test_separate_entrance_overrides_view_priority_to_now():
    updates = apply_property_priority(
        {"view_priority": "watch", "analysis": "[auto] baseline assessment"},
        {"listing_description": "Renovated home with a separate entrance to the lower level."},
    )
    assert updates["view_priority"] == "now"
    assert "TOP PRIORITY: separate entrance" in updates["analysis"]


def test_other_description_leaves_model_priority_unchanged():
    updates = apply_property_priority(
        {"view_priority": "watch", "analysis": "[auto] baseline assessment"},
        {"listing_description": "Bright kitchen and a private yard."},
    )
    assert updates["view_priority"] == "watch"


if __name__ == "__main__":
    test_separate_entrance_overrides_view_priority_to_now()
    test_other_description_leaves_model_priority_unchanged()
    print("PASS — separate entrance receives deterministic top priority")
