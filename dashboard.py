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

from kash import nl, preferences, query, status
from kash.access import Access
from kash.schema import FIELD_ORDER
from kash.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "pool.db")
ENV = os.path.join(HERE, ".env")
PREFS = os.path.join(HERE, "preferences.yaml")
MAX_TABLE = 100          # show up to the first 100 rows

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
        ("f2", "manage_access", "Bot access"),
    ]

    def __init__(self):
        super().__init__()
        self.store = Store(DB)
        self.access = Access(self.store)
        self.prefs = preferences.load(PREFS) if os.path.exists(PREFS) else {}
        self.rows: list[dict] = []

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
        log = self.query_one("#chatlog", RichLog)
        log.write("[b cyan]Robo Kash:[/]  Hi! Ask me anything about the listings — try "
                  "\"which homes are in a flood zone\" or \"cheapest under 700k\".")
        self.query_one("#chat", Input).focus()

    # --- data ---
    def set_rows(self, rows, note=""):
        self.rows = rows[:MAX_TABLE]
        table = self.query_one("#table", DataTable)
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
            try:
                filters, sort, order, limit = query.parse_command(text)
                rows = query.run(self.store, filters=filters, sort=sort, order=order,
                                 limit=min(limit, MAX_TABLE))
                self.set_rows(rows)
            except ValueError as e:
                self.sub_title = f"error: {e}"
        else:
            # a question typed in the filter bar — send it to chat instead of failing
            self.chat_send(text)

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
            backend = nl.route(self.prefs)
            reply, rows = nl.converse(message, self.store, backend=backend)
            rung = backend.chosen
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

    def reload(self):
        rows = query.run(self.store, sort="rank", limit=MAX_TABLE)
        # curated (ranked 1..N) first; scraped/unranked rows after, ordered by tier
        rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0, r.get("tier") or "Z"))
        self.set_rows(rows)
        self.render_stats()
        self.render_acquisition()


if __name__ == "__main__":
    load_dotenv(ENV)
    KashDashboard().run()
