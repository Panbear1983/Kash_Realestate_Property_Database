#!/usr/bin/env python3
"""Kash TUI dashboard — a visual terminal interface to the property pool.

    python dashboard.py

Two ways to interact, side by side:
  * FILTER bar (top)   — exact conditions, updates the table instantly:
        tier=A list_price<=750000 sort:list_price limit:10
  * CHAT box (bottom)  — plain-English conversation with your data via Codex/ChatGPT
        (the same brain the Telegram bot uses). A chat that returns listings also
        refreshes the table.

Keys: q quit · / focus filter · c focus chat · r reload · arrows move the table.
"""
from __future__ import annotations

import os
import shlex
import statistics
import webbrowser
from collections import Counter

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static, Select, TextArea, Button
from rich.markup import escape
from rich.text import Text

from kash import chat, nl, preferences, query, status
from kash.access import Access
from kash.contribution_admin import ContributionAdmin
from kash.contributions import ContributionService
from kash.contributor_sync_audit import WorkspaceSyncAuditReadModel
from kash.data_roles import DataRoles, ROLE_OWNER
from kash.review_queue import ReviewQueue
from kash.schema import FIELD_ORDER
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "pool.db")
ENV = os.path.join(HERE, ".env")
PREFS = os.path.join(HERE, "preferences.yaml")
MAX_TABLE = 2000         # safety ceiling only — the table shows the ENTIRE pool (a few
                         # hundred rows; Textual virtualizes rendering, so thousands are
                         # fine). The old value of 100 silently hid most of the database.

COLUMNS = [
    ("#", "rank"), ("T", "tier"), ("Prio", "view_priority"),
    ("Neighborhood", "neighborhood"), ("Address", "street_address"),
    ("Price", "list_price"), ("BD", "beds"), ("BA", "baths"), ("SqFt", "sqft"),
    ("$/SF", "price_per_sqft"), ("Flood", "flood_zone"),
    ("Status", "status"), ("Src", "source"),
]


def load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, int) and abs(v) >= 10000:
        return f"{v:,}"
    return str(v)


def contribution_review_access(admin, actor_id):
    """Return the local dashboard's contribution-review capability and explanation.

    The dashboard never creates a data role: its operator must already be the configured
    workflow owner.  Keeping this check outside the Textual widgets makes the security
    boundary directly testable without starting a terminal UI.
    """
    if actor_id is None:
        return False, "Contribution review disabled: KASH_CHAT_ID is not configured."
    if admin.queue.roles.role_for(actor_id) != ROLE_OWNER:
        return False, "Contribution review disabled: no owner data role is configured for KASH_CHAT_ID."
    return True, f"Contribution review enabled for owner {actor_id}."


def contributor_workspace_sync_audit_access(admin, actor_id):
    """Return the owner-only workspace sync audit capability without issuing a read."""
    if actor_id is None or admin.queue.roles.role_for(actor_id) != ROLE_OWNER:
        return False, "Workspace sync audit disabled: an owner data role is required."
    return True, f"Workspace sync audit enabled for owner {actor_id}."


class ContributionReviewController:
    """Thin UI controller: all proposal reads/actions flow through ContributionAdmin."""

    def __init__(self, admin, actor_id):
        self.admin = admin
        self.actor_id = actor_id

    def list_staged(self):
        # Deliberately no status filter: the owner sees pending, approved, and published
        # proposals needed for review, publishing, and auditable soft-retire rollback.
        return self.admin.list(self.actor_id)

    def workspace_sync_audit(self):
        enabled, _ = contributor_workspace_sync_audit_access(self.admin, self.actor_id)
        if not enabled:
            return {"enabled": False, "rows": []}
        return {"enabled": True, "rows": self.admin.list_workspace_sync_audit(self.actor_id)}

    def act(self, action, proposal_id, reason=""):
        if action in {"approve", "reject", "rollback"} and not str(reason).strip():
            raise ValueError(f"a reason is required to {action}")
        methods = {
            "approve": lambda: self.admin.approve(self.actor_id, proposal_id, reason=reason),
            "reject": lambda: self.admin.reject(self.actor_id, proposal_id, reason=reason),
            "publish": lambda: self.admin.publish(self.actor_id, proposal_id),
            "rollback": lambda: self.admin.rollback(self.actor_id, proposal_id, reason=reason),
        }
        try:
            return methods[action]()
        except KeyError as exc:
            raise ValueError("unsupported contribution action") from exc


class AccessFormScreen(ModalScreen):
    CSS = """
    AccessFormScreen { align: center middle; }
    #fbox { width: 60; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    .row { height: 3; margin-bottom: 1; }
    .label { width: 15; content-align: right middle; margin-right: 1; }
    .input { width: 1fr; }
    #f-msg-box { height: 5; margin-bottom: 1; }
    #f-greet-box { height: 8; margin-bottom: 1; }
    #f-buttons { height: 3; align: right middle; }
    """

    def __init__(self, access, existing_row=None):
        super().__init__()
        self.access = access
        self.r = existing_row or {}
        self.is_new = not bool(existing_row)

    def compose(self) -> ComposeResult:
        title = "Add User" if self.is_new else f"Edit User {self.r.get('telegram_user_id')}"
        with Vertical(id="fbox"):
            yield Static(f"[b]{title}[/b]", style="margin-bottom: 1;")
            
            with Horizontal(classes="row"):
                yield Static("User ID:", classes="label")
                uid_input = Input(str(self.r.get("telegram_user_id", "")), 
                                  placeholder="123456789", id="f-uid", classes="input")
                if not self.is_new:
                    uid_input.disabled = True
                yield uid_input
                
            with Horizontal(classes="row"):
                yield Static("Name:", classes="label")
                yield Input(str(self.r.get("name") or ""), placeholder="Jane Doe", id="f-name", classes="input")
                
            with Horizontal(classes="row"):
                yield Static("Status:", classes="label")
                yield Select([("allowed", "allowed"), ("pending", "pending"), ("denied", "denied")], 
                             value=self.r.get("status", "allowed"), id="f-status", classes="input")
                
            with Horizontal(classes="row"):
                yield Static("Access:", classes="label")
                yield Select([("read", "read")], value=self.r.get("access_level", "read"), 
                             id="f-access", classes="input", disabled=True)
                
            with Horizontal(id="f-msg-box"):
                yield Static("First Msg:", classes="label")
                msg = TextArea(str(self.r.get("first_message") or ""), id="f-msg", classes="input", read_only=True)
                msg.border_title = "Audit Log (Read Only)"
                yield msg
                
            with Horizontal(id="f-greet-box"):
                yield Static("Greeting:", classes="label")
                greet = TextArea(str(self.r.get("custom_greeting") or ""), id="f-greet", classes="input")
                greet.border_title = "Custom Bot Reply"
                yield greet

            yield Static("", id="f-error", style="color: red; margin-bottom: 1;")
            
            with Horizontal(id="f-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Done", id="btn-done", variant="primary", style="margin-left: 2;")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-cancel":
            self.dismiss(False)
            return
            
        if event.button.id == "btn-done":
            uid_str = self.query_one("#f-uid", Input).value.strip()
            if not uid_str.isdigit():
                self.query_one("#f-error", Static).update("User ID must be numeric.")
                return
            uid = int(uid_str)
            name = self.query_one("#f-name", Input).value.strip() or None
            status = self.query_one("#f-status", Select).value
            access = self.query_one("#f-access", Select).value
            greet = self.query_one("#f-greet", TextArea).text.strip() or None
            
            if self.is_new:
                self.access.add(uid, name=name, status=status, custom_greeting=greet)
            else:
                self.access.edit(uid, name=name, status=status, access_level=access, custom_greeting=greet)
                
            self.dismiss(True)


class AccessScreen(ModalScreen):
    """Manage who may chat with @Kash_Realestate_Property_bot (read-only access)."""
    CSS = """
    AccessScreen { align: center middle; }
    #abox { width: 98; height: 34; border: thick $accent; background: $surface; padding: 1 2; }
    #atitle { text-style: bold; }
    #acl { height: 1fr; margin-top: 1; margin-bottom: 1; }
    """
    BINDINGS = [
        ("escape", "close", "Close"),
        ("n", "focus_add", "Add ID"),
        ("e", "edit", "Edit"),
        ("a", "approve", "Approve"),
        ("d", "deny", "Deny"),
        ("x", "remove", "Remove"),
        ("r", "reload_list", "Refresh"),
    ]

    def __init__(self, access):
        super().__init__()
        self.access = access
        self._rows = []

    def compose(self) -> ComposeResult:
        with Vertical(id="abox"):
            yield Static("Telegram bot access — @Kash_Realestate_Property_bot (read-only)", id="atitle")
            yield Static("n add ID  ·  e edit  ·  a approve  ·  d deny  ·  x remove  ·  r refresh  ·  esc close")
            yield DataTable(id="acl", cursor_type="row", zebra_stripes=True)
            yield Static("", id="astatus")

    def on_mount(self):
        t = self.query_one("#acl", DataTable)
        t.add_columns("User ID", "Name", "Status", "Greeting", "First message")
        self.reload_list()

    def reload_list(self):
        t = self.query_one("#acl", DataTable)
        t.clear()
        self._rows = self.access.all()
        for r in self._rows:
            greet = r.get("custom_greeting")
            greet_disp = "✓ set" if greet else "-"
            t.add_row(str(r["telegram_user_id"]), r.get("name") or "-", r["status"],
                      greet_disp,
                      escape((r.get("first_message") or "")[:36]),
                      key=str(r["telegram_user_id"]))
        pending = sum(1 for r in self._rows if r["status"] == "pending")
        msg = f"{len(self._rows)} users" + (f"   [b yellow]{pending} pending[/]" if pending else "")
        self.query_one("#astatus", Static).update(msg)

    def _current(self):
        i = self.query_one("#acl", DataTable).cursor_row
        return self._rows[i]["telegram_user_id"] if (self._rows and 0 <= i < len(self._rows)) else None

    def action_approve(self):
        uid = self._current()
        if uid is not None:
            self.access.set_status(uid, "allowed")
            self.reload_list()

    def action_deny(self):
        uid = self._current()
        if uid is not None:
            self.access.set_status(uid, "denied")
            self.reload_list()

    def action_remove(self):
        uid = self._current()
        if uid is not None:
            self.access.remove(uid)
            self.reload_list()

    def action_reload_list(self):
        self.reload_list()

    def _on_form_dismissed(self, changed: bool):
        if changed:
            self.reload_list()
            self.query_one("#acl", DataTable).focus()

    def action_focus_add(self):
        self.app.push_screen(AccessFormScreen(self.access), self._on_form_dismissed)

    def action_edit(self):
        i = self.query_one("#acl", DataTable).cursor_row
        if not (self._rows and 0 <= i < len(self._rows)):
            return
        r = self._rows[i]
        self.app.push_screen(AccessFormScreen(self.access, existing_row=r), self._on_form_dismissed)

    def action_close(self):
        self.dismiss()


class WorkspaceSyncAuditScreen(ModalScreen):
    """Owner-only, read-only view of guarded contributor workspace sync outcomes."""

    CSS = """
    WorkspaceSyncAuditScreen { align: center middle; }
    #sync-audit-box { width: 105; height: 28; border: thick $accent; background: $surface; padding: 1 2; }
    #sync-audit-title { text-style: bold; }
    #sync-audit-table { height: 1fr; margin: 1 0; }
    #sync-audit-status { height: 2; }
    """
    BINDINGS = [("escape", "close", "Close"), ("r", "reload_list", "Refresh")]

    def __init__(self, admin, actor_id):
        super().__init__()
        self.controller = ContributionReviewController(admin, actor_id)
        self.available, self.access_message = contributor_workspace_sync_audit_access(admin, actor_id)

    def compose(self) -> ComposeResult:
        with Vertical(id="sync-audit-box"):
            yield Static("Contributor workspace sync audit — read only", id="sync-audit-title")
            yield Static(self.access_message, id="sync-audit-status")
            yield DataTable(id="sync-audit-table", cursor_type="row", zebra_stripes=True, disabled=not self.available)
            with Horizontal():
                yield Button("Refresh", id="sync-audit-refresh", disabled=not self.available)
                yield Button("Close", id="sync-audit-close", style="margin-left: 1;")

    def on_mount(self):
        table = self.query_one("#sync-audit-table", DataTable)
        table.add_columns("Listing identifier", "Status", "Reason", "Provenance")
        if self.available:
            self.reload_list()

    def reload_list(self):
        if not self.available:
            return
        table = self.query_one("#sync-audit-table", DataTable)
        table.clear()
        try:
            audit = self.controller.workspace_sync_audit()
        except PermissionError as exc:
            self.query_one("#sync-audit-status", Static).update(f"Workspace sync audit blocked: {exc}")
            return
        for row in audit["rows"]:
            table.add_row(row["listing_key"], row["status"], row["reason"], row["provenance"])
        self.query_one("#sync-audit-status", Static).update(
            f"{len(audit['rows'])} sync audit record(s). Internal workspace paths and details are not shown."
        )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "sync-audit-close":
            self.dismiss()
        elif event.button.id == "sync-audit-refresh":
            self.reload_list()

    def action_reload_list(self):
        self.reload_list()

    def action_close(self):
        self.dismiss()


class ContributionReviewScreen(ModalScreen):
    """Owner-only staged proposal review surface; it never writes listings itself."""

    CSS = """
    ContributionReviewScreen { align: center middle; }
    #review-box { width: 110; height: 38; border: thick $accent; background: $surface; padding: 1 2; }
    #review-title { text-style: bold; }
    #review-queue { height: 1fr; margin: 1 0; }
    #review-reason { height: 3; }
    #review-buttons { height: 3; align: right middle; }
    #review-status { height: 2; }
    """
    BINDINGS = [("escape", "close", "Close"), ("r", "reload_list", "Refresh")]

    def __init__(self, admin, actor_id):
        super().__init__()
        self.admin = admin
        self.actor_id = actor_id
        self.available, self.access_message = contribution_review_access(admin, actor_id)
        self.controller = ContributionReviewController(admin, actor_id) if self.available else None
        self._rows = []

    def compose(self) -> ComposeResult:
        with Vertical(id="review-box"):
            yield Static("Staged contributor proposals — owner-operated workflow", id="review-title")
            yield Static(self.access_message, id="review-status")
            yield DataTable(id="review-queue", cursor_type="row", zebra_stripes=True, disabled=not self.available)
            yield Input(
                placeholder="Reason required for approve, reject, and rollback (1–240 characters)",
                id="review-reason", disabled=not self.available,
            )
            with Horizontal(id="review-buttons"):
                yield Button("Approve", id="review-approve", variant="success", disabled=not self.available)
                yield Button("Reject", id="review-reject", variant="error", disabled=not self.available)
                yield Button("Publish", id="review-publish", variant="primary", disabled=not self.available)
                yield Button("Rollback", id="review-rollback", variant="warning", disabled=not self.available)
                yield Button("Refresh", id="review-refresh", disabled=not self.available)
                yield Button("Close", id="review-close", style="margin-left: 1;")

    def on_mount(self):
        table = self.query_one("#review-queue", DataTable)
        table.add_columns("ID", "Status", "Kind", "Submitted by", "Address / target", "Source", "Observed")
        if self.available:
            self.reload_list()

    @staticmethod
    def _proposal_target(proposal):
        payload = proposal.get("payload") or {}
        if not isinstance(payload, dict):
            return "-"
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        return str(fields.get("street_address") or payload.get("match_key") or "-")[:42]

    def _selected_id(self):
        index = self.query_one("#review-queue", DataTable).cursor_row
        if index is None or not (0 <= index < len(self._rows)):
            raise ValueError("select a staged proposal first")
        return self._rows[index]["id"]

    def reload_list(self):
        if not self.controller:
            return
        table = self.query_one("#review-queue", DataTable)
        table.clear()
        try:
            self._rows = self.controller.list_staged()
        except (PermissionError, ValueError) as exc:
            self._rows = []
            self.query_one("#review-status", Static).update(f"Contribution review error: {exc}")
            return
        for proposal in self._rows:
            table.add_row(
                str(proposal["id"]), proposal["status"], proposal["kind"], str(proposal["submitted_by"]),
                self._proposal_target(proposal), str(proposal.get("source_url") or "-")[:36],
                str(proposal.get("observed_at") or "-"), key=str(proposal["id"]),
            )
        self.query_one("#review-status", Static).update(
            f"{len(self._rows)} staged proposal(s). Actions are checked again by the workflow service."
        )

    def _perform(self, action):
        if not self.controller:
            return
        try:
            proposal_id = self._selected_id()
            reason = self.query_one("#review-reason", Input).value.strip()
            result = self.controller.act(action, proposal_id, reason=reason)
        except (PermissionError, ValueError) as exc:
            self.query_one("#review-status", Static).update(f"{action} blocked: {exc}")
            return
        self.query_one("#review-reason", Input).value = ""
        self.query_one("#review-status", Static).update(
            f"Proposal {proposal_id}: {result.get('status') or result.get('outcome') or 'updated'}"
        )
        self.reload_list()

    def on_button_pressed(self, event: Button.Pressed):
        button_id = event.button.id
        if button_id == "review-close":
            self.dismiss()
        elif button_id == "review-refresh":
            self.reload_list()
        elif button_id and button_id.startswith("review-"):
            self._perform(button_id.removeprefix("review-"))

    def action_reload_list(self):
        self.reload_list()

    def action_close(self):
        self.dismiss()


class KashDashboard(App):
    CSS = """
    #cmd { dock: top; border: tall $accent; }
    #main { height: 1fr; }
    #table { width: 3fr; }
    #side { width: 1fr; min-width: 30; }
    #stats { height: auto; border: round $accent; padding: 0 1; }
    #acquisition { height: auto; border: round $secondary; padding: 0 1; margin-top: 1; }
    #detailbox { height: 1fr; border: round $primary; }
    #detail { padding: 0 1; }
    #chatzone { height: 15; }
    #chatlog { height: 1fr; border: round $secondary; padding: 0 1; }
    #chat { border: tall $secondary; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("slash", "focus_cmd", "Filter"),
        ("c", "focus_chat", "Chat"),
        # r works when the table is focused; f5 / ctrl+r work even while typing in a box.
        ("r", "reload", "Reload"),
        ("f5", "reload", "Reload"),
        ("ctrl+r", "reload", "Reload"),
        ("o", "open_listing", "Open link"),
        ("n", "show_new", "New listings"),
        ("f2", "manage_access", "Bot access"),
        ("f3", "review_contributions", "Review proposals"),
        ("f4", "view_workspace_sync_audit", "Workspace sync audit"),
        ("f6", "edit_preferences", "Preferences"),
        ("f7", "edit_crm", "Tour / offer"),
    ]

    def __init__(self):
        super().__init__()
        self.store = Store(DB)
        self.access = Access(self.store)
        self.contribution_roles = DataRoles(self.store)
        self.contribution_admin = ContributionAdmin(
            ContributionService(self.store, self.contribution_roles),
            ReviewQueue(self.store, self.contribution_roles),
            WorkspaceSyncAuditReadModel(self.store, self.contribution_roles),
        )
        configured_owner = os.environ.get("KASH_CHAT_ID", "").strip()
        self.contribution_actor_id = int(configured_owner) if configured_owner.isdigit() else None
        self.prefs = preferences.load(PREFS) if os.path.exists(PREFS) else {}
        self.rows: list[dict] = []
        self._last_filter: str | None = None
        self._last_filter_note = ""
        self._pool_seen = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="FILTER: tier=A list_price<=750000  (Enter updates the table)", id="cmd")
        with Horizontal(id="main"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="stats")
                yield Static(id="acquisition")
                with VerticalScroll(id="detailbox"):
                    yield Static("Select a row to see its full record.", id="detail")
        with Vertical(id="chatzone"):
            yield RichLog(id="chatlog", wrap=True, markup=True, highlight=False)
            yield Input(placeholder="CHAT (Codex/ChatGPT): ask anything, e.g. 'which homes are in a flood zone'", id="chat")
        yield Footer()

    def on_mount(self):
        self.title = "Kash"
        self.sub_title = "Staten Island property pool"
        table = self.query_one("#table", DataTable)
        table.add_columns(*[h for h, _ in COLUMNS])
        self.reload()
        owner = os.environ.get("KASH_CHAT_ID")
        if owner and owner.isdigit():
            self.access.ensure_owner([int(owner)])
        # The local dashboard is an owner-operated surface even when no Telegram chat id is
        # configured.  This keeps its chat on the same read-only handler without exposing an
        # unauthenticated database route.
        self.access.ensure_owner([0])
        log = self.query_one("#chatlog", RichLog)
        log.write("[b cyan]Robo Kash:[/]  Hi! Ask me anything about the listings — try "
                  "\"which homes are in a flood zone\" or \"cheapest under 700k\".")
        self.query_one("#chat", Input).focus()
        # Refresh the view by itself when the nightly run / census appends while the
        # dashboard is open (r / F5 / clicking Reload in the footer still work).
        self._pool_seen = self._pool_version()
        self.set_interval(5.0, self._maybe_auto_refresh)

    # --- data ---
    def set_rows(self, rows, note="", preserve_cursor=False):
        self.rows = rows[:MAX_TABLE]
        table = self.query_one("#table", DataTable)
        old_cursor = table.cursor_row if preserve_cursor else None
        table.clear()
        for i, r in enumerate(self.rows):
            # "#" is a running row number so every row is numbered (the curated 1-35 rank
            # still shows in the detail pane). The Address is a clickable hyperlink to the
            # listing (the docx "ADDRESS / LINK"); press o/Enter to open reliably.
            url = r.get("listing_url")
            cells = [str(i + 1)]
            for _, field in COLUMNS[1:]:
                val = _cell(r.get(field))
                if field == "street_address" and url:
                    cells.append(Text(val, style=f"underline link {url}"))
                else:
                    cells.append(val)
            table.add_row(*cells, key=str(i))
        if old_cursor is not None and self.rows:
            # An auto-refresh must not yank the cursor out from under the user.
            table.move_cursor(row=min(old_cursor, len(self.rows) - 1))
        self.sub_title = note or f"{len(self.rows)} listings"

    def render_stats(self):
        rows = self.store.all()
        prices = [r["list_price"] for r in rows if r.get("list_price")]
        tier = Counter(r.get("tier") or "-" for r in rows)
        src = Counter((r.get("source") or "-").split(":")[0] for r in rows)
        flood = Counter(r.get("flood_zone") or "?" for r in rows)
        risk = sum(v for k, v in flood.items() if k not in ("X", "?"))
        lines = [f"[b]Pool[/b]   {len(rows)} listings"]
        if prices:
            lines.append(f"[b]Price[/b]  {min(prices):,} / {int(statistics.median(prices)):,} / {max(prices):,}")
        lines.append("[b]Tier[/b]   " + "  ".join(f"{k}:{v}" for k, v in sorted(tier.items())))
        lines.append(f"[b]Flood[/b]  X:{flood.get('X', 0)}   [red]risk:{risk}[/red]")
        lines.append("[b]Src[/b]    " + "  ".join(f"{k}:{v}" for k, v in src.items()))
        from kash import crm
        upcoming = crm.upcoming_viewings(self.store)[:3]
        if upcoming:
            lines.append("[b]Tours[/b]  " + "   ".join(
                f"{r['viewing_date'][5:]} {(r.get('street_address') or '')[:18]}"
                for r in upcoming))
        self.query_one("#stats", Static).update("\n".join(lines))

    def render_acquisition(self):
        summary = status.acquisition_summary(self.store, self.prefs)
        self.query_one("#acquisition", Static).update(status.format_acquisition_summary(summary))

    def render_detail(self, r):
        """Show the full record of the highlighted row — every populated field. Scrolls."""
        price = f"${r['list_price']:,}" if r.get("list_price") else ""
        head = [
            f"[b]{escape(str(r.get('street_address') or '?'))}[/b]  {r.get('zip') or ''}",
            f"[b]{price}[/b]   {r.get('tier') or '-'} · {r.get('view_priority') or '-'} · {r.get('status') or ''}",
            "[dim]press 'o' or Enter to open the listing[/dim]",
            "",
        ]
        skip = {"street_address", "zip", "list_price"}
        body = []
        for f in FIELD_ORDER:
            if f in skip:
                continue
            v = r.get(f)
            if v in (None, ""):
                continue
            if isinstance(v, int) and abs(v) >= 10000:
                v = f"{v:,}"
            body.append(f"[dim]{f}[/dim]  {escape(str(v))}")
        self.query_one("#detail", Static).update("\n".join(head + body))

    # --- events ---
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.row_key is None or event.row_key.value is None:
            return
        idx = int(event.row_key.value)
        if 0 <= idx < len(self.rows):
            self.render_detail(self.rows[idx])

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if event.input.id == "chat":
            self.chat_send(text)
            return
        # filter bar
        if not text:
            self.reload()
            return
        if any(op in text for op in ("=", "<", ">", "~")):
            self._apply_filter(text)
        else:
            # a question typed in the filter bar — send it to chat instead of failing
            self.chat_send(text)

    def _apply_filter(self, text: str, preserve_cursor: bool = False, note: str = ""):
        """Run a filter line against the pool; remembered so auto-refresh re-applies it."""
        try:
            filters, sort, order, limit = query.parse_command(text)
            rows = query.run(self.store, filters=filters, sort=sort, order=order,
                             limit=min(limit, MAX_TABLE))
        except ValueError as e:
            self.sub_title = f"error: {e}"
            return
        self._last_filter = text
        self._last_filter_note = note
        self.set_rows(rows, note=(f"{note}: {len(rows)}" if note
                                  else f"{len(rows)} match: {text}"),
                      preserve_cursor=preserve_cursor)

    def action_show_new(self):
        """Only the recent arrivals, newest first. Combined with auto-refresh this is a
        live feed: leave the dashboard here and tonight's scrape appears at the top."""
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        self._apply_filter(
            f"first_seen_date>={cutoff} sort:first_seen_date order:desc",
            note="added in the last 7 days")

    # --- chat channel (in-terminal; backend picked by the llm ladder in preferences.yaml) ---
    def chat_send(self, message: str):
        if not message:
            return
        log = self.query_one("#chatlog", RichLog)
        log.write(f"[b]you:[/]  {message}")
        self.sub_title = "thinking…"
        self.chat_worker(message)

    @work(thread=True, exclusive=True)
    def chat_worker(self, message: str):
        rung = None
        try:
            # Actor "0" matches what chat.handle records in llm_usage, so the per-actor
            # budget and ladder demotion actually apply here (as "owner" the spend was
            # invisible and the dashboard was unmetered). Also honors an F2-set model
            # override for id 0.
            backend = nl.route(self.prefs, self.access.get_model_override(0),
                               actor="0", store=self.store)
            dashboard_prefs = dict(self.prefs)
            dashboard_prefs["chat"] = {**(self.prefs.get("chat") or {}), "enabled": True}
            result = chat.handle(0, "Dashboard owner", message, store=self.store,
                                 prefs=dashboard_prefs, backend=backend)
            reply, rows, rung = result.text, list(result.rows), result.backend
            # Market comparisons deliberately carry no rows, so the current table stays in
            # place. Ordinary database queries retain the existing table-refresh behavior.
        except Exception as e:  # noqa: BLE001
            reply, rows = (f"error: {e}", [])
        self.call_from_thread(self.chat_result, reply, rows, rung)

    def chat_result(self, reply: str, rows: list, rung: str = None):
        log = self.query_one("#chatlog", RichLog)
        tag = f" [dim]({rung})[/dim]" if rung else ""
        log.write(f"[b cyan]Robo Kash:[/]{tag}  {reply}")
        if rows:
            self.set_rows(rows, note=f"{len(rows)} listings (from chat)")
            log.write(f"[dim]→ table updated with {min(len(rows), MAX_TABLE)} listing(s)[/dim]")
        else:
            self.sub_title = f"{len(self.rows)} listings"

    # --- actions ---
    def action_focus_cmd(self):
        self.query_one("#cmd", Input).focus()

    def action_focus_chat(self):
        self.query_one("#chat", Input).focus()

    def action_reload(self):
        self.reload()

    def action_open_listing(self):
        self._open_current()

    def action_manage_access(self):
        self.push_screen(AccessScreen(self.access))

    def action_review_contributions(self):
        self.push_screen(ContributionReviewScreen(self.contribution_admin, self.contribution_actor_id))

    def action_view_workspace_sync_audit(self):
        self.push_screen(WorkspaceSyncAuditScreen(self.contribution_admin, self.contribution_actor_id))

    def action_edit_crm(self):
        """Record a viewing / offer / note against the highlighted listing."""
        from kash.crm_screen import CrmScreen
        from kash.dedup import match_key
        table = self.query_one("#table", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.rows)):
            self.notify("Highlight a listing first.")
            return
        key = match_key(self.rows[idx])
        if not key:
            self.notify("That row has no stable key to write against.")
            return
        self.push_screen(CrmScreen(self.store, key), self._on_crm_dismissed)

    def _on_crm_dismissed(self, saved: bool):
        if saved:
            self.reload()

    def action_edit_preferences(self):
        from kash.prefs_screen import PreferencesScreen
        self.push_screen(PreferencesScreen(PREFS), self._on_prefs_dismissed)

    def _on_prefs_dismissed(self, saved: bool):
        if not saved:
            return
        # The yaml is the single source of truth — re-read it so the dashboard's filters,
        # acquisition panel, and chat routing use the new values immediately.
        self.prefs = preferences.load(PREFS)
        self.render_acquisition()
        self.notify("Preferences reloaded — the scrape applies them on its next run.")

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self._open_current()

    def _open_current(self):
        table = self.query_one("#table", DataTable)
        idx = table.cursor_row
        if idx is None or not (0 <= idx < len(self.rows)):
            return
        url = self.rows[idx].get("listing_url")
        if url:
            webbrowser.open(url)
            self.sub_title = f"opened {self.rows[idx].get('street_address')}"
        else:
            self.sub_title = "no listing link for this row"

    def reload(self, preserve_cursor: bool = False):
        """Show the ENTIRE pool, default ranked view; clears any active filter."""
        self._last_filter = None
        self._last_filter_note = ""
        rows = query.run(self.store, sort="rank", limit=MAX_TABLE)
        # curated (ranked 1..N) first; scraped/unranked rows after, ordered by tier
        rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0, r.get("tier") or "Z"))
        total = self.store.count()
        self.set_rows(rows, f"{len(rows)} of {total} listings" if len(rows) < total else "",
                      preserve_cursor=preserve_cursor)
        self.render_stats()
        self.render_acquisition()

    # --- auto-refresh: the nightly run and the census append while the dashboard is open ---
    def _pool_version(self):
        """Cheap change signal: row count + last changelog id. The Telegram bridge writes
        other tables in this same file, so file mtime alone would refresh spuriously."""
        try:
            n = self.store.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            c = self.store.conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM changelog").fetchone()[0]
            return (n, c)
        except Exception:  # noqa: BLE001 — a locked/busy read must not crash the UI
            return None

    def _maybe_auto_refresh(self):
        version = self._pool_version()
        if version is None or version == self._pool_seen:
            return
        self._pool_seen = version
        if self._last_filter:
            self._apply_filter(self._last_filter, preserve_cursor=True,
                               note=self._last_filter_note)
            self.render_stats()
            self.render_acquisition()
        else:
            self.reload(preserve_cursor=True)
        self.notify("Pool changed on disk — view refreshed")


if __name__ == "__main__":
    load_dotenv(ENV)
    KashDashboard().run()
