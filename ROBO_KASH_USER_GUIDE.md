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

### Search more than one area at once

```text
Show me homes in Great Kills or Annadale under $800k.
```

Either-or searches work for neighborhoods, home types, and statuses.

### Ask about the database

```text
What is the average price of the entire database?
```

```text
What is the average price in Great Kills?
```

```text
How many active listings are there in each neighborhood?
```

```text
What is the average price by property type?
```

Counts, averages, minimums, and maximums work — overall, for one area, or broken down per neighborhood, property type, status, or tier. Kash labels the important distinction between active listings and all stored listings, and between asking/list price and verified sale price.

### Ask for judgment about results

```text
Show 3-bedroom homes under $750k in Great Kills and tell me which is the best value.
```

```text
Compare the two-family homes under $800k for me.
```

When you ask for a comparison or a recommendation about your search results, Kash lists the homes and adds a short reasoned take — based only on the listings it just showed you, never invented facts.

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

Misspellings are fine: "homes in Anadale" finds Annadale, and Kash notes the correction so you can see what it assumed.

## Follow-up messages

Kash remembers your last search for about 30 minutes, so natural follow-ups work:

```text
Which of those have 3 bedrooms?
Tell me about the second one.
Open #3.
Only show the cheaper ones.
```

For about 5 minutes after a search, you can also refine it without any special phrasing — "actually make that under $700k" continues the same search.

If Kash asks you one clarifying question — say, "Which neighborhood and what budget?" — just answer it plainly: "Great Kills", "under 700k", or "3 beds" completes the search you started. Memory stores only the shape of your search (the filters), never your message text.

## Optional shortcuts for power users

These are optional; normal language is preferred.

```text
show 25 Northfield Ave
```

```text
filter status=active sort:list_price limit:5
```

```text
usage
```

Shows how much of today's model budget you have used.

```text
model claude_cli
```

Pins your replies to one model backend (`model auto` returns to the default ladder).

```text
help
```

Contributors can also just send a listing sheet (PDF, Word, or screenshot) to the chat — Kash reads it, previews what it found, and stages it for owner review. Additional `/propose` and `/mine` commands are covered in the contributor guides in `docs/`.

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
