# Memory

What the agent keeps about you between chats, how it is filed, and what you can do with it. The model
is the memory of the chat apps you already use: titled pages you can read, correct and delete, each
saying where every line of it came from.

## Records and entries

A **record** is one note: a line of text with a category (preference, person, project, fact,
procedure), topics, a version, and the scope it lives in — your global memory, or one project's. The
agent writes records with `Remember`, reads them with `Recall` and drops them with `Forget`; you write
them on the Memory screen, in Telegram with `/remember`, or by import.

Every record belongs to exactly one **entry**, the titled page it is read in. Entries come in three
sections:

| Section | Entries | Scope |
|---|---|---|
| You | Preferences (how you want the agent to work, in your words) and Profile (who you are) | global |
| Topic | People, Recent work (where a note waits until something better files it), and one page per subject that earns one | global |
| Area | One per project, holding that project's records; it outlives the project as a free area | project |

The rule that files a record is one pure function (`daedalus/memory/entries.py`, `target_for`): a
project-scoped record goes to its project's area; a preference to Preferences; a note about you to
Profile and about someone else to People; anything else to the first of its topics that already has
a page, else to Recent work. The agent's `Remember`, your quick add, a bullet you add on an entry's
page and an import all go through one door, `MemoryEntries.remember`, so no record is ever kept
without an entry and a **source**.

A source row (`memory_sources`) says who taught a record: a chat (with the place in it), you, an
import, the background pass, or the migration that filed the records kept before entries. A record
two chats taught has two rows. Records are JSON blobs, so "which records did this chat teach" is
answered from that table by one indexed query.

## Versions, locks, tombstones and history

- An entry's `version` steps on every change to it or to any of its bullets. Every edit is sent with
  the version it was made against, and a newer one refuses it with 409: an edit made while the agent
  added a bullet is not allowed to overwrite it silently.
- A bullet you wrote or edited is **locked**: the background pass may suggest a change to it, never
  make one. You can lock and unlock any bullet.
- An **archived** bullet is kept, folded under the entry's older bullets, and not handed to the agent.
- A bullet you delete leaves a **tombstone** — a fingerprint of its text, blind to case, accents and
  punctuation — so the background pass does not learn it again from the same chats.
- Every change is one row of `memory_history` with the records as they were before and after, plus
  what else it removed: the entries it deleted, the source rows it dropped, the tombstones it wrote,
  the entries it made. Undo puts all of it back together. Deleting an entry with forty bullets, or a
  change in words that edits three and adds two, is one undo.

## Deleting a chat

Deleting one of your chats takes out of memory what only that chat taught. A record whose every
source is that chat goes; one another chat also backs, that you wrote, or that came by import stays,
and so does a locked one. The chat's source rows go in either case, so a record kept because a second
chat backs it goes when that chat is deleted too. The whole removal is one history entry, and its
undo restores the records with every source row. Nothing is tombstoned: saying the same thing in a
new chat is new evidence.

Only your own conversations count. A subagent, a staff member or a side window is cleaned up by the
host when its work is done, and the notes it left in a project's memory are the team's; they stay.

## Private chats

A private chat is kept like any chat, but it is never learned from, never searched or embedded, and
is handed no memory; the memory tools refuse in it. Set when the chat is made, never changed after.

## The Memory screen

`/app/memory` lists the entries grouped as you, projects, topics and recent work, each with its
summary and when it last changed, under a status pill (on, paused, not learning, today's budget used,
learning now, or when the background pass last ran). The field under the list is the **quick add**:
the line is classified as `Remember` classifies it and filed by the same rule. `/app/memory/<id>` is
one entry: its summary and title edited in place, its bullets with the chat each was learned in (a
link to the place it was said), edit, lock, archive and forget, each with Undo. The field under an
entry takes an instruction in your words — "I moved to Lisbon" — and a model proposes operations on
that entry: add a bullet, change one, remove one, retitle or re-summarise the page. Nothing changes
until you approve the diff; every change can be unticked first, and Apply is sent with the entry's
version. The bullets go to the model fenced as data, so a bullet that reads like an instruction is
text to edit, never an order; a proposed bullet that reads like an instruction to an AI is refused.

The record grid — by category, by topic, merges, the tidying's suggestions — stays as the **All facts**
segment (`?view=facts`). A turn whose `Remember` saved something shows a **Saved to memory** chip that
opens the entry; in Telegram the answer gets one line saying the same.

## Settings → Memory

| Setting | Key | What it does |
|---|---|---|
| Generate memory from chats | `memory.generate` | The background pass learns from your chats |
| Search and reference chats | `memory.search_chats` | `ChatSearch` and `RecentChats` are offered |
| Pause memory | `memory.paused` | Keep everything, hand nothing to the agent, learn nothing new; chats held during the pause are never learned from later |
| Memory model | `memory.synthesis_preset` | Any configured preset; empty = the categorising model's, else the default |
| Daily budget for learning | `memory.synthesis_usd_per_day` | What the background pass may spend in a day |
| Include sensitive topics | `memory.sensitive_allowed` | Off, nothing on health, finances, intimate life, identity documents or precise location is kept; turning a subject off counts what memory holds on it and offers to remove it |
| Reset memory | — | Deletes every entry, record, source, tombstone and the history; asks for the word typed out |

Notes you add yourself are never refused for a sensitive subject: the setting guards what the agent
keeps on its own.

The background pass that learns from chats is its own piece of work and reads these keys when it
lands; until then `generate`, the memory model and the budget are kept and shown, the status pill says
memory learns from what you tell it, and memory changes when you or the agent write to it. The memory
model already serves the change in words.

## Export and import

`GET /api/memory/export.zip` is a zip of Markdown pages, one per entry under `you/`, `topics/` and
`areas/`, each with front matter (title, summary, section, project, updated, aliases) and one
`- bullet` per record with its sources in a trailing comment, plus `memory.json` with the entries,
records and sources exactly as kept.

An import is untrusted text however it arrives. The import sheet gives a prompt to run in another
assistant ("list every memory you have about me in one code block, one per line, as
`[date saved] - memory content`, verbatim"); you paste its answer, or pick a Daedalus export or a
Markdown page. Parsing is code, not a model, so nothing in the text decides how it is read: inside a
code block when there is one, one note per line, the date kept. Every note is filed by the same rule
and scanned for text that addresses an AI agent, for sensitive subjects, for a note memory already
holds and for one you deleted before. The preview shows what was found; an agent-directed note can
never be ticked, and the apply scans again on the server and refuses it whatever the client sent,
because memory is handed to the agent in later chats. One history entry undoes the whole import.

## Telegram

- `/memory` — the state, the You pages, the counts, and a link to the app for the rest.
- `/remember <text>` — the quick add, with the same filing, lock and history.
- `/private <title>` — a new private chat.
- An answer whose turn saved something gets one line: `Saved to memory · <entries>`.

## Routes

| Route | |
|---|---|
| `GET /api/memory/entries`, `GET /api/memory/entries/{id}` | the entries with counts and the status; one entry with its records and their sources |
| `POST /api/memory/entries` | a new topic or free area |
| `PATCH /api/memory/entries/{id}` | title, summary, aliases, with `version` (409 when stale) |
| `DELETE /api/memory/entries/{id}?version=` | the entry and its bullets as one undoable change; a You page keeps itself |
| `POST /api/memory/entries/{id}/bullets` | a bullet filed in that entry |
| `POST /api/memory/entries/{id}/instruct` | `apply: false` asks the memory model for a preview; `apply: true` applies the approved operations with `version` |
| `POST /api/memory/quick` | the quick add |
| `PATCH /api/memory/{id}`, `POST /api/memory/{id}/mark`, `DELETE /api/memory/{id}` | edit a bullet (with `version`), lock or archive it, forget it |
| `GET /api/memory/status` | what the status pill reads |
| `GET /api/memory/export.zip` | the export |
| `POST /api/memory/import/preview`, `POST /api/memory/import/apply` | the import, parsed and scanned, then filed |
| `POST /api/memory/sensitive` | count, then remove, what memory holds on subjects no longer allowed |
| `POST /api/memory/reset` | delete everything, with the confirmation word |
| `GET /api/memory/history`, `POST /api/memory/history/{id}/undo` | the history and its undo |
