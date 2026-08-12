"""The dashboard's per-listing CRM panel — record a tour, an offer, a rating, a note.

Rendering is driven by crm.FIELDS; validation and writing belong to kash/crm.py, so this
file holds no rules. Dismisses True when something was written, so the dashboard can reload
the row it is showing.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from kash import crm


def _wid(name: str) -> str:
    return "crm-" + name.replace("_", "-")


_BOOL_OPTIONS = [("yes", "yes"), ("no", "no"), ("—", "")]


class CrmScreen(ModalScreen[bool]):
    def __init__(self, store, key: str):
        super().__init__()
        self.store = store
        self.key = key
        self.row = store.get(key) or {}

    CSS = """
    CrmScreen { align: center middle; }
    #crmbox { width: 70; height: auto; max-height: 90%; border: thick $accent;
              background: $surface; padding: 0 2; }
    .crmrow { height: auto; }
    .crmrow Label { width: 28; padding-top: 1; }
    .crmrow Input, .crmrow Select { width: 30; }
    #crm-notes { height: 6; border: round $secondary; }
    #crm-errors { color: $error; height: auto; }
    #crm-buttons { height: 3; align-horizontal: right; }
    #crm-buttons Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        address = self.row.get("street_address") or self.key
        price = self.row.get("list_price")
        with VerticalScroll(id="crmbox"):
            yield Static(f"[b]{address}[/b]" + (f"   ${price:,}" if price else ""))
            yield Static(f"[dim]{crm.summary(self.row)}[/dim]")
            for f in crm.FIELDS:
                current = self.row.get(f.name)
                if f.kind == "text":
                    yield Label(f.label)
                    yield TextArea(str(current or ""), id=_wid(f.name))
                    continue
                with Horizontal(classes="crmrow"):
                    yield Label(f.label)
                    if f.kind == "choice":
                        value = str(current or "none")
                        yield Select([(o, o) for o in f.options], value=value,
                                     allow_blank=False, id=_wid(f.name))
                    elif f.kind == "bool":
                        value = "" if current is None else ("yes" if current else "no")
                        yield Select(_BOOL_OPTIONS, value=value, allow_blank=False,
                                     id=_wid(f.name))
                    else:
                        yield Input(value="" if current is None else str(current),
                                    id=_wid(f.name))
            yield Static("", id="crm-errors")
            yield Static("[dim]Recorded here, a listing is never auto-aged to off_market.[/dim]")
            with Horizontal(id="crm-buttons"):
                yield Button("Save", variant="primary", id="crm-save")
                yield Button("Cancel", id="crm-cancel")

    def _collect(self) -> dict:
        values = {}
        for f in crm.FIELDS:
            widget = self.query_one(f"#{_wid(f.name)}")
            values[f.name] = widget.text if f.kind == "text" else widget.value
        return values

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "crm-cancel":
            self.dismiss(False)
            return
        if event.button.id != "crm-save":
            return
        try:
            result = crm.apply(self.store, self.key, self._collect())
        except crm.CrmError as e:
            self.query_one("#crm-errors", Static).update(f"! {e}")
            return
        if result["changed"]:
            self.app.notify("; ".join(result["notes"])[:200])
        self.dismiss(bool(result["changed"]))

    def action_cancel(self) -> None:
        self.dismiss(False)
