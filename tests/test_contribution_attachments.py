#!/usr/bin/env python3
"""Quarantined contributor attachments are bounded evidence, never listing writes."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kash.contribution_attachments import AttachmentService  # noqa: E402
from kash.contributions import ContributionService  # noqa: E402
from kash.data_roles import DataRoles, ROLE_CONTRIBUTOR  # noqa: E402
from kash.store import Store  # noqa: E402

OWNER, CONTRIBUTOR = 1, 2
PDF = b"%PDF-1.7\nfixture evidence"
LEGACY_DOC = bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy-document-fixture"


def _services(tmp):
    store = Store(":memory:")
    roles = DataRoles(store); roles.bootstrap_owner(OWNER)
    roles.grant(OWNER, CONTRIBUTOR, ROLE_CONTRIBUTOR, reason="approved")
    contributions = ContributionService(store, roles)
    proposal = contributions.submit_add(CONTRIBUTOR, {"street_address": "10 Example Road", "zip": "10301"},
                                        source_url="https://example.com/10", observed_at="2026-08-06")
    return store, roles, proposal, AttachmentService(store, roles, quarantine_dir=tmp)


def test_authorized_contributor_can_quarantine_pdf_evidence_without_listing_write():
    with tempfile.TemporaryDirectory() as tmp:
        store, _roles, proposal, attachments = _services(tmp)
        before = store.count()
        record = attachments.stage(CONTRIBUTOR, proposal["id"], "evidence.pdf", PDF)
        assert record["status"] == "quarantined"
        assert record["sha256"] == hashlib.sha256(PDF).hexdigest()
        assert Path(record["path"]).parent == Path(tmp)
        assert Path(record["path"]).name != "evidence.pdf"
        assert store.count() == before


def test_legacy_doc_is_quarantined_for_manual_review_not_auto_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        _store, _roles, proposal, attachments = _services(tmp)
        record = attachments.stage(CONTRIBUTOR, proposal["id"], "evidence.doc", LEGACY_DOC)
        assert record["status"] == "manual_review"
        assert record["extraction"] == "disabled"


def test_mismatched_or_unsafe_attachment_is_rejected_before_storage():
    with tempfile.TemporaryDirectory() as tmp:
        _store, _roles, proposal, attachments = _services(tmp)
        for name, content in [("fake.pdf", b"#!/bin/sh\nrm -rf /"), ("archive.zip", b"PK\x03\x04")]:
            try:
                attachments.stage(CONTRIBUTOR, proposal["id"], name, content)
            except ValueError:
                pass
            else:
                raise AssertionError(f"unsafe attachment accepted: {name}")
        assert not list(Path(tmp).iterdir())


def test_expired_evidence_is_deleted_from_quarantine_but_keeps_audit_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        store, _roles, proposal, attachments = _services(tmp)
        record = attachments.stage(CONTRIBUTOR, proposal["id"], "evidence.pdf", PDF)
        stored_name = Path(record["path"]).name
        store.conn.execute("UPDATE contribution_attachments SET created_at='2000-01-01 00:00:00'")
        store.conn.commit()
        assert attachments.purge_expired() == 1
        assert not Path(record["path"]).exists()
        status = store.conn.execute("SELECT status FROM contribution_attachments WHERE stored_name=?", (stored_name,)).fetchone()[0]
        assert status == "expired"


if __name__ == "__main__":
    for test in [test_authorized_contributor_can_quarantine_pdf_evidence_without_listing_write,
                 test_legacy_doc_is_quarantined_for_manual_review_not_auto_parsed,
                 test_mismatched_or_unsafe_attachment_is_rejected_before_storage,
                 test_expired_evidence_is_deleted_from_quarantine_but_keeps_audit_metadata]:
        test(); print(f"  ok  {test.__name__}")
    print("4 passed — contributor evidence stays quarantined and non-publishing")
