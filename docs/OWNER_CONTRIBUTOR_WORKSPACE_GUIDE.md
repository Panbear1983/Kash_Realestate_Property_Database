# Owner Guide: Contributor Workspace → Kash Shared Database

## What this gives the contributor

The contributor has an independent, private Kash workspace. They can query, add, edit, retire, restore, and soft-delete **their own** workspace records through the Kash Telegram bot.

They do **not** receive `pool.db`, SQL access, filesystem paths, credentials, private notes, other contributors’ records, reviewer controls, or notification settings.

## How a new listing reaches the shared database

1. The contributor sends `/mine add` with a public HTTPS source, observed date, address/ZIP, and listing fields.
2. Kash writes it to their private workspace immediately.
3. Kash evaluates the record deterministically:
   - active listing;
   - first revision/new addition;
   - valid public HTTPS URL;
   - observed date;
   - stable address/MLS identity;
   - no existing shared-pool match.
4. If all checks pass, it auto-syncs to `pool.db` through the controlled `Store.upsert()` path.
5. Kash records workspace revision, source provenance, sync status, and a shared audit entry.

The contributor never writes directly to `pool.db`.

## Cases requiring your review

These do **not** overwrite the shared pool automatically:

- a duplicate/shared identity conflict;
- invalid or incomplete source evidence;
- edits to an already-added workspace record;
- retire, restore, or soft-delete actions;
- document/evidence-related assertions.

They are recorded as `held_for_review` in the contributor workspace sync audit. In the dashboard, press **F4 — Workspace sync audit** to inspect the safe summary. The view intentionally excludes filesystem paths, opaque sync keys, raw document contents, and internal error details.

## Owner setup / access lifecycle

Only run owner-local setup after code verification and a backup:

1. Confirm the user has an active `contributor` data role and normal Telegram access.
2. Create/enable exactly one workspace for that contributor through the owner-controlled application service.
3. Keep `auto_sync_additions` enabled only for the canary contributor.
4. If access must stop, disable the workspace and revoke the contributor role. Do not delete the workspace database; the audit trail must remain.

## Canary checklist

1. Have the contributor add one low-risk public listing.
2. Confirm `/mine list` shows it only to the contributor.
3. Confirm the reply is either `synced` or `held_for_review` with a reason.
4. If synced, verify one shared listing and provenance/audit entry.
5. Have them edit the workspace price; verify `PENDING OWNER REVIEW` and no shared overwrite.
6. Have them attach a small evidence file using `/attach-mine <listing key>`; confirm it remains quarantined and is not parsed or auto-published.
7. Record the canary outcome. Do not expand to more contributors until it passes.

## Reversal

- **Workspace only:** contributor can restore a soft-deleted record; owner can disable the workspace.
- **Shared addition:** use the controlled owner rollback/recovery procedure and preserve audit history. Never manually delete an audited shared row to hide history.
