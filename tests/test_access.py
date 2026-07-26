#!/usr/bin/env python3
"""Access-list editing tests, including dashboard first-message input."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Removed _parse_access_input from dashboard
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


def test_custom_greeting():
    store = Store(":memory:")
    access = Access(store)

    assert access.get_greeting(101) is None
    access.add(101, "Jane", custom_greeting="Hi Jane!")
    assert access.get_greeting(101) == "Hi Jane!"

    access.edit(101, custom_greeting="Welcome back Jane")
    assert access.get_greeting(101) == "Welcome back Jane"
    
    # omitting custom_greeting should not clear it
    access.edit(101, name="Jane Doe")
    assert access.get_greeting(101) == "Welcome back Jane"
    
    # explicitly clearing
    access.edit(101, custom_greeting="")
    assert access.get_greeting(101) is None
    
    store.close()

if __name__ == "__main__":
    test_first_message_editing()
    test_custom_greeting()
    print("PASS — access first-message add/edit and custom greeting behavior OK")
