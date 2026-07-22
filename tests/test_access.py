#!/usr/bin/env python3
"""Access-list editing tests, including dashboard first-message input."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import _parse_access_input  # noqa: E402
from kash.access import Access              # noqa: E402
from kash.store import Store                # noqa: E402


def test_first_message_editing():
    store = Store(":memory:")
    access = Access(store)

    assert access.request(101, "Jane", "Original hello") == "pending"
    access.edit(101, status="allowed", first_message="Custom hello")
    row = access.all()[0]
    assert row["status"] == "allowed"
    assert row["first_message"] == "Custom hello"

    # Omitting the field preserves it; an explicit empty value clears it.
    access.edit(101, name="Jane Doe")
    assert access.all()[0]["first_message"] == "Custom hello"
    access.edit(101, first_message="")
    assert access.all()[0]["first_message"] == ""

    access.add(202, "John", first_message="Pre-authorized greeting")
    john = next(r for r in access.all() if r["telegram_user_id"] == 202)
    assert john["first_message"] == "Pre-authorized greeting"
    store.close()


def test_dashboard_access_input():
    parsed = _parse_access_input(
        '123456789 name="Jane Doe" status=allowed access=read '
        'message="Hello from the dashboard"'
    )
    assert parsed == {
        "user_id": 123456789,
        "name": "Jane Doe",
        "status": "allowed",
        "access_level": "read",
        "first_message": "Hello from the dashboard",
    }

    alias = _parse_access_input("123 first_message='Custom text'")
    assert alias["first_message"] == "Custom text"
    cleared = _parse_access_input("123 message='' ")
    assert cleared["first_message"] == ""


if __name__ == "__main__":
    test_first_message_editing()
    test_dashboard_access_input()
    print("PASS — access first-message add/edit/parser behavior OK")
