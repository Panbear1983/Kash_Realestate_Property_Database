# Kash Contributor Workspace Guide

You have a private Kash workspace. You can work with your own listings directly; you cannot access the shared Kash database, other users’ workspaces, settings, credentials, or SQL.

## Add a listing

Send this to the Kash Telegram bot:

```text
/mine add
source_url: https://public-listing-url.example/...
observed_at: 2026-08-07
street_address: 10 Example Road
zip: 10301
list_price: 700000
status: active
beds: 3
baths: 2
```

Required:

```text
source_url — a public HTTPS listing URL
observed_at — YYYY-MM-DD
street_address or MLS identity
zip (when using an address)
```

The reply says either:

```text
Sync status: synced
```

or:

```text
Sync status: held_for_review
```

`Synced` means a clean new listing was copied to the shared Kash pool through Kash’s controlled system. `held_for_review` means the item remains safely in your workspace and the owner will review the reason.

## View your workspace

```text
/mine list
/mine show <listing key>
```

Copy the listing key shown by `/mine list` when you need to edit, retire, restore, delete, or attach evidence.

## Change your workspace listing

```text
/mine edit <listing key>
list_price: 695000
```

```text
/mine retire <listing key>
reason: no longer active at source
```

```text
/mine restore <listing key>
reason: relisted at source
```

```text
/mine delete <listing key>
reason: duplicate record
```

These changes affect **your workspace** immediately and are auditable. They do not overwrite the shared pool automatically; the owner must review shared-pool changes.

## Upload a document into a private draft

You can upload a `.doc` or `.docx` file to create a private, editable draft rather than typing every field.

1. Attach a `.doc` or `.docx` directly to the Kash Telegram chat with this caption:

```text
/attach-mine
```

You do **not** need to create a listing first.

2. Kash replies with an **Attachment ID**. Create a draft:

```text
/mine draft <attachment id>
```

3. Review pending drafts:

```text
/mine drafts
/mine draft show <draft id>
```

4. Correct or add fields before confirmation. Send only one `field: value` line per field:

```text
/mine draft edit <draft id>
source_url: https://public-listing.example/...
observed_at: 2026-08-07
street_address: 10 Example Road
zip: 10301
list_price: 700000
status: active
beds: 3
baths: 2
```

5. Confirm only after reviewing the result:

```text
/mine confirm <draft id>
```

A confirmation is safe to retry if Telegram times out: resend the same command; Kash reuses the same private workspace record rather than creating another one.

Or discard the private draft:

```text
/mine discard <draft id>
```

Kash extracts only explicit allow-listed `field: value` lines locally. It does not execute macros, run a shell, send document contents to an AI/chat model, or import a document directly into the shared database. Confirming is required before any workspace listing is created. A confirmed draft still needs a public HTTPS `source_url`, `observed_at`, and stable listing identity before the normal guarded sync can evaluate it.

## Attach supporting evidence

Attach a PDF, JPG, PNG, DOCX, or legacy DOC to the Kash Telegram chat with this caption:

```text
/attach-mine <listing key>
```

Rules:

- Maximum file size: 10 MiB.
- For keyed `/attach-mine <listing key>` evidence, a legacy `.doc` remains manual-review evidence only. Use standalone `/attach-mine` to create a private document draft.
- Files are quarantined with an audit checksum.
- Kash does not execute files, read them into the chat model, or extract their contents into listing fields.
- A document is evidence only; it never publishes or changes a listing by itself.

## Important boundaries

```text
✓ You control records in your own workspace.
✓ You can submit clean new public listings for automatic guarded sync.
✗ You cannot query/edit other workspaces or raw pool.db.
✗ You cannot use SQL, receive credentials, or change bot settings.
✗ You cannot auto-overwrite an existing shared listing.
✗ Do not submit private information, passwords, API keys, or non-public documents.
```

If a command is rejected, send `/mine help` and correct the structured fields. For a held item, keep the public source URL current and wait for the owner’s review.
