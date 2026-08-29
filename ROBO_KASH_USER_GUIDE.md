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

## Hear the answer instead of reading it

Robo Kash can speak. Send `/voice on` and every answer arrives twice — as the normal text
message, then as a voice message you can play anywhere, including with the phone in your
pocket. `/voice off` goes back to text only.

```text
/voice on          turn spoken replies on
/voice off         text only
/voice status      what is set right now
/voice voices      the list of voices to choose from
/voice set eric    switch voice (first name is enough)
/voice try ryan    hear a sample without switching
/voice speed +10   speak 10 percent faster; -50 to +50
/voice test        hear a sample in the current voice
```

There are two kinds of voice. The natural ones (Andrew, Christopher, Eric, Ryan and so on)
sound like a person reading. The robot ones are the classic Mac synthesizers — `Fred` is
the closest available match to Stephen Hawking's speech machine, and `/voice set hawking`
is accepted as a name for it. Stephen Hawking's actual voice was a DECtalk unit, which is
not obtainable on this Mac, so Fred is a near relative rather than the real thing.

The spoken version is a shortened, listenable rendering: code, tables and web addresses are
left out because they are unbearable to hear, and it stops on a whole sentence rather than
running for minutes. The text message beside it is always the complete answer, with the
clickable listing links.

Once voice is on, the daily 7am property report speaks too. It arrives as the usual written
messages, then a short spoken briefing at the end: how much changed overnight, the biggest
price cut with the address, and the top home on your list. It is deliberately short and
deliberately not a reading of the written report — a list of addresses and prices read out
loud is unusable. The written messages remain the full record, with the clickable links.

If speech ever fails, the text answer still arrives — the voice message is the only thing
you lose.

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
