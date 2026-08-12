"""The dashboard's Preferences screen — edit preferences.yaml without leaving the TUI.

Rendering is driven entirely by prefs_editor.FIELDS; this file holds no field knowledge.
Effect timing, stated where the user can see it: dashboard filters and Robo Kash pick the
change up immediately (the app reloads prefs on save), the scrape query on the next nightly
run. Recipients are shown read-only — a typo there cuts off alerts or messages a stranger.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from kash import preferences, prefs_editor as pe


def _wid(path: str) -> str:
    return "pref-" + path.replace(".", "-")


def _oid(path: str, option: str) -> str:
    return "opt-" + path.replace(".", "-") + "-" + option


class PreferencesScreen(ModalScreen[bool]):
    """Dismisses True when a save happened (the app then reloads prefs), False otherwise."""

    CSS = """
    PreferencesScreen { align: center middle; }
    #prefbox { width: 76; height: 85%; border: thick $accent; background: $surface;
               padding: 0 2; }
    .group { margin-top: 1; text-style: bold; color: $accent; }
    .fieldrow { height: auto; }
    .fieldrow Label { width: 26; padding-top: 1; }
    .fieldrow Input { width: 24; }
    .multi { height: auto; layout: grid; grid-size: 4; grid-columns: 1fr 1fr 1fr 1fr; }
    #pref-errors { color: $error; height: auto; }
    #pref-note { color: $text-muted; height: auto; }
    #pref-buttons { height: 3; align-horizontal: right; }
    #pref-buttons Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, prefs_path: str):
        super().__init__()
        self.prefs_path = prefs_path
        self.prefs = preferences.load(prefs_path)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="prefbox"):
            yield Static("[b]Preferences[/b] — saved to preferences.yaml "
                         "(backup kept in archive/prefs_backups/)")
            group = None
            for f in pe.FIELDS:
                if f.group != group:
                    group = f.group
                    yield Static(group, classes="group")
                if f.kind == "multi":
                    yield Label(f.label)
                    current = {str(v) for v in (pe.get_value(self.prefs, f.path) or [])}
                    with Horizontal(classes="multi"):
                        for opt in pe.universe_for(f, self.prefs):
                            yield Checkbox(opt, value=opt in current,
                                           id=_oid(f.path, opt))
                elif f.kind == "choice":
                    with Horizontal(classes="fieldrow"):
                        yield Label(f.label)
                        current = str(pe.get_value(self.prefs, f.path) or f.universe[0])
                        yield Select([(o, o) for o in f.universe], value=current,
                                     allow_blank=False, id=_wid(f.path))
                else:
                    with Horizontal(classes="fieldrow"):
                        yield Label(f.label)
                        v = pe.get_value(self.prefs, f.path)
                        yield Input(value="" if v is None else str(v), id=_wid(f.path))
            yield Static("Read-only", classes="group")
            for path in pe.READ_ONLY_PATHS:
                yield Static(f"  {path}: {pe.get_value(self.prefs, path)}")
            yield Static("", id="pref-errors")
            yield Static("Applies immediately in the dashboard and Robo Kash; "
                         "the scrape query picks it up on the next nightly run.",
                         id="pref-note")
            with Horizontal(id="pref-buttons"):
                yield Button("Save", variant="primary", id="pref-save")
                yield Button("Cancel", id="pref-cancel")

    def _collect(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for f in pe.FIELDS:
            if f.kind == "multi":
                values[f.path] = [
                    opt for opt in pe.universe_for(f, self.prefs)
                    if self.query_one(f"#{_oid(f.path, opt)}", Checkbox).value
                ]
            elif f.kind == "choice":
                values[f.path] = self.query_one(f"#{_wid(f.path)}", Select).value
            else:
                values[f.path] = self.query_one(f"#{_wid(f.path)}", Input).value
        return values

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pref-cancel":
            self.dismiss(False)
            return
        if event.button.id != "pref-save":
            return
        new, errors = pe.apply(self.prefs, self._collect())
        box = self.query_one("#pref-errors", Static)
        if errors:
            box.update("\n".join(f"! {e}" for e in errors))
            return
        backup = pe.save_with_backup(new, self.prefs_path)
        self.app.notify("Preferences saved"
                        + (f" (backup: {backup.rsplit('/', 1)[-1]})" if backup else ""))
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
