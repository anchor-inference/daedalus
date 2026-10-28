---
name: rich-answers
description: Syntax for answer cards (:::card, :::entity) and [^](url) citations. Load before writing cards.
---
# Cards and citations in an answer

The Mini App draws these as cards and source chips, with a "N sources" button under the answer that
lists what you cited, what else you read and the searches you made. Telegram and any other client get
tidy text instead. You write nothing for the sources list: it is built from your citations and tools.

## When

Cards: two or more products, repositories, extensions, places or services you compare or recommend,
or one company or project the operator asked about. Not for an ordinary answer, a single fact, a
how-to, code, or anything you did not look up — plain Markdown says those better.

Citations: after a sentence that rests on a page you opened or a search result you got this turn.

## Card

```
:::card
title: 1. Pantum P2500NW
url: https://kaspi.kz/shop/p/pantum-p2500nw/
image: https://…/p2500nw.jpg
badge: My pick
meta: ≈ 45 000–59 000 ₸ · mono laser · Wi-Fi
- 22 pages a minute
- starter cartridge for about 1 600 pages
links: [Kaspi](https://…) · [DNS](https://…)
note: DNS shows 59 270 ₸ with delivery today; check your address.
:::
```

- Every key is optional; give at least `title`. Lines that are not keys are the body (Markdown):
  one or two sentences or up to four bullets.
- `title`: numbered (`1. `) in a ranked list. `url`: the item's own page.
- `image`: only an address a tool gave you — an `image:` line of a WebSearch hit or a WebFetch
  result, or a picture you saw on the page. Never guess one; without it the site's icon is shown.
- `badge`: at most one card in a list, a two-word verdict ("My pick", "Best value").
- `meta`: one line, facts joined with ` · ` (price, licence, stars, rating, users).
- `links`: where to get or read it, `[Label](url)` joined with ` · `. `note`: one muted caveat.
- Cards that follow each other (blank lines between are fine) form one list.

`:::entity` takes the same keys for one company, project or person: `meta` is its kind and place,
the body one line of what it does, `links` its site and code. Put details after it as a table.

## Citation

`Claim.[^](https://page)` — a chip named after the site. Several side by side,
`[^](https://a) [^](https://b)`, become one chip with "+1". `[^GitHub](https://…)` names it yourself.
Cite only pages from this turn's tools, never an address you did not see.
