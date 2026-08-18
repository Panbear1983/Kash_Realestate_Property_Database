# Robo Kash — How to Use It

Robo Kash is a read-only Staten Island property assistant. Talk to it like a person: **you do not need SQL, database fields, or technical commands.**

## Start with what matters

A good request usually names some combination of:

- **where** — neighborhood, ZIP, or address;
- **what** — home type, bedrooms, bathrooms, or other requirements;
- **budget** — a maximum price or rough target;
- **priority** — cheapest, newest, rental potential, listing links, etc.

You do not need every detail. Add more only when the first answer is too broad.

## Examples you can copy

### Find properties

```text
Show me active homes under $750k.
```

```text
Find two-family homes in Great Kills around $700k.
```

```text
Show active listings with at least 3 bedrooms and 2 bathrooms.
```

```text
What are the cheapest active listings?
```

### Ask for links

```text
Show me the link for 25 Northfield Ave.
```

```text
Find active two-family homes under $800k and include the listing links.
```

```text
Give me the Zillow links for the cheapest active listings.
```

Kash provides a clickable link when a valid stored listing link is available. If the database does not have one, it should say so rather than inventing a link.

### Ask about the database

```text
What is the average price of the entire database?
```

```text
What is the cheapest active listing in the database?
```

```text
Show active listings in Great Kills sorted from lowest price to highest.
```

Kash labels the important distinction between active listings and all stored listings, and between asking/list price and verified sale price.

### Ask practical questions

```text
What should I look for in a two-family property?
```

```text
Explain cap rate in simple terms.
```

```text
What does a separate entrance usually mean for a rental property?
```

### Ask about current external context

```text
What are current mortgage-rate conditions?
```

```text
Find current market context for Staten Island properties.
```

Current web answers should include sources. Kash may decline a market comparison when the available sources use incompatible measures—for example, a local average active-listing price versus a State median closed-sale price.

## If the first answer is not right

Rephrase with one or two concrete details. For example:

```text
Instead of:  Show me something good.
Try:         Show active two-family homes under $750k in Great Kills,
             cheapest first, with listing links.
```

```text
Instead of:  Find a rental property.
Try:         Find active two-family homes around $700k that may have rental potential.
```

Kash is being improved to understand more flexible language. At present, clear location, price, property type, and desired output produce the most reliable database results.

## Follow-up messages

Private short-term conversation memory is **still under construction**. For now, repeat the essential conditions when asking a follow-up.

```text
Instead of:  Give me the links for those.
Use today:   Give me the links for active two-family homes under $750k in Great Kills.
```

When the session-memory feature is live, Kash will support natural follow-ups such as:

```text
Give me the links for those.
Only show the cheaper ones.
Sort those by price.
```

## Optional shortcuts for power users

These are optional; normal language is preferred.

```text
show 25 Northfield Ave
```

```text
filter status=active sort:list_price limit:5
```

```text
help
```

## What Kash will not do

Kash is read-only. It will not:

- edit, delete, import, or otherwise modify listings;
- contact an agent, seller, or owner;
- schedule appointments or submit offers;
- reveal private notes, secrets, or restricted listing fields;
- invent listing links or property facts when the database does not contain them.

## Quick request formula

Use this whenever useful:

```text
Show [property type] in [area] under/around [budget],
with [requirements], sorted by [priority], and include [links/details].
```

Example:

```text
Show active two-family homes in Great Kills around $700k,
with at least 3 bedrooms, cheapest first, and include listing links.
```
