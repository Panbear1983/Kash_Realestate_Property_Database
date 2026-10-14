# Kash_Realestate_Property - agent rules

**Memory wing: `kash_realestate_property`.** All agents (Claude Code, Hermes, Codex, Antigravity) share ONE long-term
memory, the mempalace, reached through the `mempalace_*` tools. Full rules and wing table:
`~/.mempalace/MEMORY_PROTOCOL.md`.

Kash's property project (Kash is the repo's real owner), served through Hermes_Telegram_Bridge.

## Every task in this repo
1. Start: `mempalace_search(<what you are about to do>, wing="kash_realestate_property")` before planning.
2. Finish: one `mempalace_add_drawer(wing="kash_realestate_property", room=<decisions|backend|bugs|reference>)`:
   what was done, what was verified, what is still open, Peter's words verbatim where they matter.
   Dated facts (bug fixed, decision made, thing deployed) also go to `mempalace_kg_add`.
3. Never file secrets, tokens or financial figures.
4. The Hermes kanban board is live work state, not memory; do not copy cards into the palace.

## Standing rules for this repo
- (none recorded yet)
