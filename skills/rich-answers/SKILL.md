---
name: rich-answers
description: Syntax for answer cards (:::card, :::entity), flows (:::flow) and [^](url) citations. Load before writing them.
---
# Cards, flows and citations in an answer

The Mini App draws these as cards and source chips, with a "N sources" button under the answer that
lists what you cited, what else you read and the searches you made; it draws flows and mermaid fences
as diagrams. Telegram and any other client get tidy text instead. You write nothing for the sources list: it is built from your citations and tools.

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

## Flow

For a process, a pipeline, a decision path or a small architecture the operator asked to see, or one
that reads better as a picture than as a list. Not for two or three plain steps (a list) or for data
(a table).

```
:::flow 💡 Concept of a small pilot project
💬 Customer — writes in Telegram or on the site
↓
🤖 ==AI assistant== — answers from the knowledge base
↓ cannot answer
✅ Solved — about 80% of questions → 🎧 Operator — takes the rest
note: Two weeks on one channel, then measure the share answered.
:::
```

- One line is one row. Nodes side by side are joined by ` → `, ` ← `, ` ↔ ` or ` | ` (no arrow);
  two or three to a row.
- A node: an emoji, a short title, then ` — ` and a subtitle of a few words. The app draws these
  emoji as line icons: 💬 🤖 ✅ ❌ ⚠️ 🎧 👤 📄 📁 🌐 🔍 🗄 🖥 ⚙️ 🔧 💡 🔒 🔑 ⏱ 📅 📊 🛒 🏠 📞 📧 ☁️ ⚡ 🔗 🛡 🔔 📦 🧩.
- Between rows `↓` is the default and may be left out; put `↑` or `↕` (both ways) on its own line
  when it differs. Words after the arrow label it.
- `==…==` around one node fills it: the key step or the answer. At most one per flow.
- The opener's rest is the panel's title, an emoji first giving it an icon; `note:` is a muted caption.
- Telegram shows these lines as they are, so they must read well as text.

## Mermaid

What rows cannot hold — branches that merge, loops, messages between parties, states, a timeline —
goes in a ```` ```mermaid ```` fence. The app draws it in its own colours with the code a click away;
Telegram shows the code. Keep it to about fifteen nodes with short labels, quote a label with
punctuation (`A["Pay (card)"]`), and leave out `click`, `style` and `classDef`: the app themes it.
