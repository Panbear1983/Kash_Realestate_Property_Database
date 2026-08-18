# Kash Contributor Guide (Staged Workflow)

> **Current status:** This guide describes the controlled contributor workflow being built. Do not submit listing changes through ordinary Kash Telegram chat until the protected contribution interface is live and you have been assigned the `contributor` role.

## What a contributor can do

A contributor can submit a **proposal** to:

- add a public listing;
- correct an existing listing; or
- soft-retire a listing that is no longer active.

A proposal is not a direct database edit. It is validated and held for review before publication.

## What to include

Every proposal needs:

```text
Public HTTPS listing URL
Observed date (YYYY-MM-DD)
Action: add / correct / retire
```

Use an approved public listing source. Do not provide documents, logins, private notes, phone numbers, financial documents, or database files.

## Add a listing

Use this information:

```text
Action: Add listing
Source URL: https://...
Observed date: YYYY-MM-DD
Address: [street address]
ZIP: [ZIP]
Asking price: [amount]
Status: active / pending / sold / off_market
Beds: [optional]
Baths: [optional]
Property type: [optional]
```

Example:

```text
Action: Add listing
Source URL: https://www.example.com/listing/123
Observed date: 2026-08-05
Address: 10 Example Road
ZIP: 10301
Asking price: $700,000
Status: active
Beds: 3
Baths: 2.5
Property type: single-family detached
```

## Correct a listing

Use the existing listing link or identifier and state only what changed.

```text
Action: Correct listing
Listing URL/ID: [existing Kash listing link or identifier]
Source URL: https://...
Observed date: YYYY-MM-DD
Changed field: [field]
New value: [value]
```

Example:

```text
Action: Correct listing
Listing URL/ID: [existing listing]
Source URL: https://www.example.com/listing/123
Observed date: 2026-08-05
Changed field: Asking price
New value: $695,000
```

The system will reject the publish if the listing changed after the correction was submitted, so an older proposal cannot overwrite newer information.

## Retire a listing

Retire means archive / mark no longer active. It does **not** permanently delete the record.

```text
Action: Retire listing
Listing URL/ID: [existing Kash listing link or identifier]
Source URL: https://...
Observed date: YYYY-MM-DD
Reason: [why the listing should no longer be active]
```

Example:

```text
Action: Retire listing
Listing URL/ID: [existing listing]
Source URL: https://www.example.com/listing/123
Observed date: 2026-08-05
Reason: The public source now marks the listing sold.
```

## What happens after submission

```text
Contributor submits
→ validation checks source, date, fields, and identity
→ proposal stays pending
→ reviewer approves, rejects, or asks for correction
→ publisher applies an approved change
→ before/after audit evidence is retained
```

For a retired listing, an owner can reinstate its previous status through an audited rollback.

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
