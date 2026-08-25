# Kash Contributor Guide (Staged Workflow)

As an assigned `contributor` you can submit a **proposal** to:

- add a public listing;
- correct an existing listing; or
- soft-retire a listing that is no longer active.

A proposal is not a direct database edit. It is validated and held for the owner's review
before publication, and you get a Telegram message when it is approved, rejected, or
published.

## Upload a document (easiest)

Just send the listing sheet to the Kash Telegram chat — a PDF, a Word document, or a
screenshot. No caption or commands needed.

Kash quarantines the file, reads what it can, and replies with a preview of the draft it
built — including whether it would submit an **add** (new address), a **correction**
(the pool already tracks the address; you'll see `field: old -> new` for what changed),
or a **retire** (the document says the listing is sold or off the market).

Then:

```text
/mine submit 3                 send draft 3 to the owner for review
/mine draft edit 3             fix anything first (field: value lines below the command)
street_address: 10 Example Road
list_price: 700000
/mine discard 3                drop it
```

Screenshots can't be read for text — the reply will ask you to type the details with
`/mine draft edit`, and your image still travels with the proposal as evidence.

Every draft needs a street address + ZIP (or an MLS number), a public `https` source
link, and an observed date (pre-filled with today when the document doesn't state one).
The preview lists anything still missing.

## Typed proposals (`/propose`)

The structured alternative. Field names are the exact database names, values are plain
(`700000`, not `$700,000`), one `field: value` per line.

### Add

```text
/propose add
source_url: https://www.example.com/listing/123
observed_at: 2026-08-05
street_address: 10 Example Road
zip: 10301
list_price: 700000
status: active
beds: 3
baths: 2.5
property_type: sf_detached
```

### Correct

State only what changed. `match_key` is the listing's identity — ask Kash to
`show 10 Example Road` and use the key it reports, or derive it as
`addr:<lowercased street>|<zip>`.

```text
/propose correct
match_key: addr:10 example road|10301
source_url: https://www.example.com/listing/123
observed_at: 2026-08-05
list_price: 695000
```

The system rejects the publish if the listing changed after the correction was
submitted, so an older proposal cannot overwrite newer information.

### Retire

Retire means archive / mark no longer active. It does **not** permanently delete the
record.

```text
/propose retire
match_key: addr:10 example road|10301
source_url: https://www.example.com/listing/123
observed_at: 2026-08-05
reason: The public source now marks the listing sold.
```

### Evidence

Attach a file to a pending proposal you own by sending it with the caption
`/attach <proposal number>`.

## What happens after submission

```text
Contributor submits (upload or /propose)
→ validation checks source, date, fields, and identity
→ proposal stays pending; the owner is notified
→ reviewer approves or rejects (you are notified either way, with the reason)
→ publisher applies an approved change (you are notified on publish)
→ before/after audit evidence is retained
```

For a retired listing, an owner can reinstate its previous status through an audited
rollback.

## Current boundaries

Contributors cannot:

```text
publish their own work
approve their own work
hard-delete listings
run SQL or access pool.db
view private notes or secrets
change user roles, bot settings, providers, or notification recipients
```
