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

When the background pass held notes back for your word, a line above the entries says how many, and
each entry's row carries a count badge. The line opens the **review**: the notes grouped by the entry
they are for or by the chat read that filed them, each row with the chat it came from (linked to the
message) and what you said there. A group is accepted or dismissed whole with one tap, as one change
with Undo; a dismissed note's words are tombstoned (`dismissed`), so no later pass proposes them again.
Opened from an entry, the review shows that entry's notes only.

The record grid — by category, by topic, merges, the tidying's suggestions — stays as the **All facts**
segment (`?view=facts`). A turn whose `Remember` saved something shows a **Saved to memory** chip that
opens the entry; in Telegram the answer gets one line saying the same.

## Settings → Memory

| Setting | Key | What it does |
|---|---|---|
| Generate memory from chats | `memory.generate` | The background pass learns from your chats |
| Search and reference chats | `memory.search_chats` | `ChatSearch` and `RecentChats` are offered |
| Pause memory | `memory.paused` | Keep everything, hand nothing to the agent, refuse the agent's `Remember` and `Recall`, learn nothing new; chats held during the pause are never learned from later. Your own notes still save |
| Memory model | `memory.synthesis_preset` | Any configured preset; empty = the recommended cheap model when configured, else the categorising model's, the summariser's or a middle preset — the page says which one runs |
| Daily budget for learning | `memory.synthesis_usd_per_day` | What memory's model calls may spend in a day (UTC); a free model is not stopped by it; 0 = no background pass |
| Learning from chats | — | When the last pass ran, what it added, updated, confirmed, archived and held back, today's spend, chats it could not read; **Update now** reads every chat with something unread |
| Read every chat again | — | Every chat's watermark goes back to the start and the pass reads them all, to learn what an earlier pass dropped; what memory holds is confirmed rather than written twice, and tombstoned notes stay away |
| Include sensitive topics | `memory.sensitive_allowed` | Off, nothing on health, finances, intimate life, identity documents or precise location is kept; turning a subject off counts what memory holds on it and offers to remove it |
| Reset memory | — | Deletes every entry, record, source, tombstone and the history; asks for the word typed out |

Notes you add yourself are never refused for a sensitive subject: the setting guards what the agent
keeps on its own.

Pausing stops what memory does on its own — the block handed to the agent, the agent's `Remember`
and `Recall`, the background pass — and marks what is said meanwhile as read, so it is never learned
later. What you do by hand is yours and still works: the quick add, a bullet added or edited on the
Memory screen, `/remember` in Telegram, and the agent's `Forget` (removing a note uses nothing).

## Learning from chats

`daedalus/memory/synthesis.py` is the background pass. A chat is read when it has been quiet for ten
minutes after at least two new messages of yours (or a long stretch of them), in a nightly sweep at
03:30 your time, and when you press **Update now**. A chat is never read while one of its runs is in
progress. The first start does not sweep the backlog by itself: the first nightly sweep is the next
night, and **Update now** reads everything at once.

Only your own conversations are read: not a private chat, a subagent's, a team member's, a schedule's,
a heartbeat's, a prompt coach's or a side question's session, nor one another agent started. Imported
sessions are read: they are your past chats held elsewhere. Inside a chat only your messages (not a
schedule's turn, a loop's tick or a report) and the agent's final answer before each of them are read;
tool calls and results never are.

The model answers with operations — add a bullet, update or retire one it was shown, make a topic or
an area, write an entry's summary. Code decides what happens to each:

- words addressed to an AI agent are refused (imported chats are fenced as data besides);
- immigration or residence status, a criminal record, an identity number, a password with its value
  and anything already redacted are **never** kept, whatever the switches say;
- a sensitive subject you did not allow is refused, by the word lists and the model's own label;
- a bullet you removed (its tombstone) is not learned again;
- a preference must quote your words exactly, or it is refused — the pass cannot invent "prefers short
  answers" from how you behave;
- a bullet naming an identifier you never wrote (usually the agent's own choice of a port, a default
  or a library) is held as a suggestion, not written;
- a change to a bullet you wrote or edited (locked) becomes a suggestion; the pass never makes it;
- a fact memory already holds gains the chat as a second source instead of a second bullet;
- an entry keeps at most 20 preferences, 12 profile notes, 20 notes per topic and 30 per area (60
  entries in all). A new note past the cap archives the least valuable unlocked note — the one
  confirmed longest ago, the fewest sources among equals; never a locked one or yours — and when every
  note is locked, the new one is held for your word instead. Archived notes are not handed to the
  agent but `Recall` still finds them.

After the operations, one small follow-up call writes a one-to-two-sentence summary for each topic and
area the pass touched (unless the model already wrote one, or you wrote the summary yourself). The
injected block lists every other entry by that summary.

Everything one pass over one chat changed is one history entry, **Learned from “<chat>”**, whose undo
takes it all back — the notes it archived and the summaries it wrote included — and withdraws the
suggestions it filed. A chat's watermark moves only after a part of it was applied, so a failed call reads
the same messages again later. Each call is booked in Usage as `memory`; once today's memory spend
reaches the budget, the pass waits for tomorrow (a model that costs nothing carries on).

The model is the one you chose; with none chosen, `opencode.deepseek-v4.1-flash` when it is configured
(the metered `deepseek.deepseek-v4-flash` is its fallback), chosen on a benchmark over real chats for
valid answers at the first try, precision and cost.

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
| `GET /api/memory/status` | what the status pill and the settings read: the model and why, the last pass's report |
| `POST /api/memory/synthesis/run` | read every chat with something unread now (409 while paused, off or without budget) |
| `POST /api/memory/synthesis/reread` | read every chat again from its start (409 as above) |
| `GET /api/memory/suggestions` | what waits: each with its entry, chat, quote and the pass that filed it |
| `POST /api/memory/suggestions/decide` | `{action: apply \| dismiss, ids}`: a group decided as one undoable change |
| `POST /api/memory/suggestions/{id}/apply`, `…/dismiss` | one decided; a dismissal answers with its history entry |
| `GET /api/memory/export.zip` | the export |
| `POST /api/memory/import/preview`, `POST /api/memory/import/apply` | the import, parsed and scanned, then filed |
| `POST /api/memory/sensitive` | count, then remove, what memory holds on subjects no longer allowed |
| `POST /api/memory/reset` | delete everything, with the confirmation word |
| `GET /api/memory/history`, `POST /api/memory/history/{id}/undo` | the history and its undo |
