#!/usr/bin/env python3
"""Telegram proposal text is deterministic, typed, and never reaches the LLM chat path."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contribution_commands import ContributionCommands  # noqa: E402
from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR = 1, 2


def _handler():
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved")
    return ContributionCommands(ContributionService(store, roles))


def test_contributor_can_submit_typed_add_proposal_from_telegram_template():
    handler = _handler()
    reply = handler.try_handle(CONTRIBUTOR, """/propose add
source_url: https://example.com/listing/10
observed_at: 2026-08-06
street_address: 10 Example Road
zip: 10301
list_price: 700000
status: active""")
    assert reply and reply.startswith("Proposal #1 submitted")
    assert "pending review" in reply


def test_non_command_returns_none_and_noncontributor_cannot_submit():
    handler = _handler()
    assert handler.try_handle(CONTRIBUTOR, "find homes under 700k") is None
    reply = handler.try_handle(99, """/propose add
source_url: https://example.com/listing/10
observed_at: 2026-08-06
street_address: 10 Example Road
zip: 10301""")
    assert reply == "Contributor access is required to submit a proposal."


def test_correction_and_retire_templates_remain_staged_not_listing_writes():
    handler = _handler()
    correction = handler.try_handle(CONTRIBUTOR, """/propose correct
source_url: https://example.com/listing/10
observed_at: 2026-08-06
match_key: 10 Example Road|10301
list_price: 650000""")
    retire = handler.try_handle(CONTRIBUTOR, """/propose retire
source_url: https://example.com/listing/10
observed_at: 2026-08-06
match_key: 10 Example Road|10301
reason: Listing is no longer active""")
    assert correction == "Proposal was not accepted: correction target does not exist. Send /propose help for the template."
    assert retire == "Proposal #1 submitted and pending review. It has not changed any listing."


if __name__ == "__main__":
    for test in [test_contributor_can_submit_typed_add_proposal_from_telegram_template,
                 test_non_command_returns_none_and_noncontributor_cannot_submit,
                 test_correction_and_retire_templates_remain_staged_not_listing_writes]:
        test(); print(f"  ok  {test.__name__}")
    print("3 passed — Telegram proposal text is typed and role-gated")
