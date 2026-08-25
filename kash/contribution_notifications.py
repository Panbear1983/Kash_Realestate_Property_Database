"""Telegram pings around the contribution workflow — submit and decision, both ways.

Pure application glue: the actual push is an injected callable with
``run_update.push_telegram``'s signature (text, chat_ids) -> {chat_id: outcome}, so tests
stay offline with a recorder and production wires the real sender. Every send is
best-effort — a push failure never blocks or alters a submit or a review action — and
ledgered in ``notification_delivery`` (PK recipient+kind) so retries and restarts cannot
double-ping. A recipient is marked only after the push reports "sent", mirroring
scripts/broadcast_upgrade.py.
"""
from __future__ import annotations

import json


def _summary(kind: str, payload: dict) -> str:
    if kind == "add":
        bits = [payload.get("street_address") or "(no address)", payload.get("zip") or ""]
        price = payload.get("list_price")
        if price:
            try:
                bits.append(f"${int(price):,}")
            except (TypeError, ValueError):
                bits.append(str(price))
        return ", ".join(b for b in bits if b)
    if kind == "correct":
        fields = ", ".join(sorted((payload.get("fields") or {}).keys())) or "fields"
        return f"{payload.get('match_key')} — {fields}"
    if kind == "retire":
        return f"{payload.get('match_key')} — {payload.get('reason') or 'retire'}"
    return kind


class ProposalNotifier:
    def __init__(self, store, roles, push):
        self.store = store
        self.roles = roles
        self.push = push

    def _proposal(self, proposal_id):
        row = self.store.conn.execute(
            "SELECT kind, submitted_by, payload_json FROM listing_proposals WHERE id=?",
            (int(proposal_id),),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[2])
        except (TypeError, ValueError):
            payload = {}
        return {"kind": row[0], "submitted_by": int(row[1]), "payload": payload}

    def _send(self, recipient, kind_key, text) -> None:
        """One ledgered, best-effort push. Never raises."""
        try:
            recipient = int(recipient)
            if recipient <= 0:                       # dashboard's internal actor 0 etc.
                return
            if self.store.notification_sent(recipient, kind_key):
                return
            outcome = self.push(text, [recipient]).get(recipient)
            if outcome == "sent":
                self.store.mark_notification_sent(recipient, kind_key)
        except Exception:  # noqa: BLE001 — notifying must never break the action
            pass

    def submitted(self, proposal_id) -> None:
        """Ping every owner that a proposal awaits review (skip an owner-submitter)."""
        try:
            proposal = self._proposal(proposal_id)
            if proposal is None:
                return
            text = (f"Kash proposal #{int(proposal_id)} ({proposal['kind']}): "
                    f"{_summary(proposal['kind'], proposal['payload'])} — review in the "
                    "dashboard (F3).")
            for owner_id in self.roles.owner_ids():
                if int(owner_id) == proposal["submitted_by"]:
                    continue
                self._send(owner_id, f"proposal:{int(proposal_id)}:submitted", text)
        except Exception:  # noqa: BLE001
            pass

    def decision(self, proposal_id, decision: str, *, reason: str = "",
                 actor_id=None) -> None:
        """Tell the submitter their proposal was approved / rejected / published."""
        try:
            if decision not in ("approved", "rejected", "published"):
                return
            proposal = self._proposal(proposal_id)
            if proposal is None:
                return
            submitter = proposal["submitted_by"]
            if actor_id is not None and int(actor_id) == submitter:
                return                                # self-decisions need no ping
            what = f"#{int(proposal_id)} ({proposal['kind']} — " \
                   f"{_summary(proposal['kind'], proposal['payload'])})"
            if decision == "published":
                text = f"Your proposal {what} was published to the shared pool."
            else:
                text = f"Your proposal {what} was {decision}"
                text += f": {reason}" if reason else "."
            self._send(submitter, f"proposal:{int(proposal_id)}:{decision}", text)
        except Exception:  # noqa: BLE001
            pass
