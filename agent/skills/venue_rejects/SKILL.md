---
name: venue_rejects
description: What each venue rejection means, how wide its blast radius is, and what to do
  about it without an operator - the difference between an order problem, a pair problem and
  a venue problem, and which rejects are normal noise that must NOT stop trading.
when_to_use: When an order or a deploy is rejected, when a controller will not place, when
  the bot logs show a venue error code, or when deciding whether a failure justifies dropping
  one order, one pair, or a whole venue. Read this BEFORE stopping anything.
source: agent:funding_builders_cup
---

# Venue rejects: blast radius, and what not to panic about

You have no operator. Every error resolves here, and the only question that matters is **blast
radius**: is this the venue, the pair, or just this order? Act at that level and no wider.

You have no operator. Every error resolves here, and the only question that matters is **blast
radius**: is this the venue, the pair, or just this order? Read the error, classify it, act at that
level and no wider. `manage_bots` and the bot logs carry the venue's message.

| What you see | Blast radius | What to do |
|---|---|---|
| **post-only would cross** / "could not be executed as maker" / `-5022` | **this order only** | **Nothing. This is normal.** The book moved between quote and placement, the venue refused rather than filling you as taker, and the executor re-quotes on its own next cycle. Expected in a tight book. Do NOT stop the pair over it |
| **reduce-only reject** | this order only | Usually benign - the other leg closed the position first. Ignore unless it repeats |
| **min notional** / **precision / tick size** | this order only | Our sizing is wrong, not the venue's fault. Raise `min_notional` or the per-order slice so it clears the venue minimum with room, and redeploy that pair |
| **leverage or position mode not set** | **the venue, for this pair** | The account is not configured. You cannot fix this from a controller - leverage is an account setting. Journal it clearly, skip that pair, keep trading everything else |
| **max notional / tier cap** | **the pair** | The position is too big for this venue at this leverage. Redeploy that pair smaller; do not touch the others |
| **open interest cap** | **the pair, that venue only** | The venue's OI for this asset is full - nobody can increase. Nothing to fix and not your size's fault. Try the **other venue as maker**, or drop the base this tick |
| **insufficient margin** | **the venue** | Stop opening on that venue. Read `margin_book`; free margin only if it is genuinely short |
| **auth / IP not authorized** | **the venue** | Credentials or IP. Nothing you can do from here. Journal it and stop deploying to that venue - and treat the book as unknown, exactly as for an unreadable venue |
| **rate limit** / **clock skew / recvWindow** | **transient** | Back off. Deploy fewer pairs this tick and continue. Do not treat as a venue failure |
| **anything unrecognised** | unknown | Quote the venue's raw message in the journal, skip that pair this tick, keep the others running |

**Do not be over-cautious.** The failure that costs the most here is not a bad trade, it is an
agent that stops trading over noise. Specifically:

- **A post-only cross is not a failure.** It is the mechanism working. If you halt on these you
  will halt constantly, because they happen every time the book moves.
- **A rejected order is not a rejected pair, and a rejected pair is not a rejected venue.** Act at
  the narrowest level the error justifies.
- **One pair halting is normal.** Let the others run. Never stop the whole bot to deal with one
  base.
- **A transient error deserves a retry, not a shutdown.**

There are exactly **three** things that stop you deploying at all, and all three are in Step 2:
a venue is unreadable, a venue is below the margin floor, or you cannot tell whether the book is
hedged. Everything else is handled per-pair and the run continues.

## The reflex to resist

An agent that stops trading over noise costs far more than a bad fill. Post-only crosses happen
every time the book moves; if they halt you, you halt constantly and trade nothing for 48 hours.

Classify first, then act at the narrowest level the error justifies.
