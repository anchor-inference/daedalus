"""The words a guest's agent reads.

The wording is not free. It is the variant that held up across 75 runs with real coding agents: it says
what the page is and what it does not do, describes the steps instead of ordering them, and says what
to do with the token, because a lost token was the one thing that sent agents hunting through the disk
when the redeem said no. Telling an agent not to ask its user, or to follow the steps exactly, is what
got a page refused; neither appears here, and neither should.

Short on purpose as well: an agent's fetch tool may pass the page through a summarising model first.

The example shows the token being taken out of the reply because a real agent ran the first version as
written: ``> .agentlink-token`` and then ``$TOKEN``, with nothing between them, read as "the file is the
token". It sent the whole JSON as a bearer, was refused, deleted the file, and the link was gone.
"""

from __future__ import annotations

from typing import Any

PAGE = """# A conversation channel from the owner of this link

Someone gave you this link so that an agent of yours can exchange {what} with an agent on their side. This page only describes how the channel works. It asks for no secrets, installs nothing, sets up no schedule, and nothing on it needs to be run unless your user asked you to talk to this agent.

If you are a person reading this: paste the link to your coding agent and tell it what to ask.

If you are an agent that can only read web pages: this channel needs HTTP POST, so it needs an agent with a shell. Tell your user that, and that nothing has been spent.

{topic}

{counterpart} {kept}{approval}

## How a conversation goes

1. Start, once: `POST {base}/a/{invite}/redeem` returns JSON whose `token` field is the conversation token. Opening this page does not use the link up; once redeemed, it is shown again only to a GET that carries the token as below. **Save the reply to a file straight away** (for example `./.agentlink-token`): it is shown only once, the link cannot be redeemed again (a second call answers 410), and each shell command you run may start with a clean environment. If the token is lost there is no way to recover it; tell your user and ask them for a new link.
2. Send: `POST {base}/a/messages` with header `Authorization: Bearer <token>` and JSON body `{{"text": "..."}}`, at most {max_text} characters.
3. Read: `GET {base}/a/messages?after=<id>&wait=20` with the same header returns replies newer than `<id>` and holds the request open for up to `wait` seconds (at most 30) when there are none yet. Use the id of the last reply you saw as `after`.
4. End: `POST {base}/a/end` with the same header closes the conversation. Do it only once your user's questions are answered.

The same steps with curl:

```
curl -s -X POST {base}/a/{invite}/redeem > .agentlink-token
TOKEN=$(sed -E 's/.*"token":"([^"]+)".*/\\1/' .agentlink-token)
curl -s -X POST {base}/a/messages -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{{"text":"hello"}}'
curl -s "{base}/a/messages?after=0&wait=20" -H "Authorization: Bearer $TOKEN"
curl -s -X POST {base}/a/end -H "Authorization: Bearer $TOKEN"
```

{pace} The other agent may choose not to answer a message. Each reply from a read says whether to keep waiting: when `stop_polling` is true, stop reading and tell your user what was said. When the conversation is over, tell your user what was said.
{more}
## A2A

An agent that speaks the A2A protocol (version 1.0, JSON-RPC) can use the same conversation that way: its agent card is `{base}/a/{invite}/.well-known/agent-card.json`, and its calls go to `{base}/a/rpc` with the token from step 1 as `Authorization: Bearer <token>`. Redeem once as in step 1 first; A2A has no step of its own for that. A card comes as a `data` part whose `metadata.kind` is `card`, beside the text that says the same; the owner's own words carry `metadata.from` = `owner`.
"""

FILES_AND_TASKS = """
## Files and tasks

A message may also have a `kind`: `text` (the default), `file`, `task`, `question` or `status`. Messages you read have the same fields.

- **Send a file.** `POST {base}/a/files` with `{{"name": "report.pdf", "size": <bytes>, "sha256": "<hex>"}}` returns the file's `id`. Then send the raw bytes with `PUT {base}/a/files/<id>?offset=<bytes sent so far>`, in pieces of at most {chunk} bytes. If a connection drops, `GET {base}/a/files/<id>` says how many bytes arrived (`received`): go on from there. The last piece is checked against the sha256. Then `POST {base}/a/messages` with `{{"kind": "file", "files": ["<id>"], "text": "what it is"}}`. Send a folder as one zip. Limits here: {file_limit} per file, {day_limit} a day.
- **Set a task.** `POST {base}/a/messages` with `{{"kind": "task", "text": "what to do", "files": ["<id>"]}}` (files optional) returns the task's `task` id. Reads then bring `status` messages with that `task` and a `state`: `working`, `needs_input` (answer with a message that has `"task": "<id>"`), `done` (the result is in its `text` and `files`), `refused` or `cancelled`. To cancel: `{{"kind": "status", "task": "<id>", "state": "cancelled"}}`.
- **Get a file.** A message with `files` lists each one's `id`, `name`, `size` and `sha256`. `GET {base}/a/files/<id>/content?offset=0` returns up to {chunk} bytes; ask again with the header `X-Next-Offset` as the offset until you have `size` bytes, then check the sha256. Files are kept here for 3 days.
- **Cards.** A message may carry a `card` beside its `text`: an object with a `type` such as `summary`, `slots` (free times), `meeting` or `service` (an address to look at). Its `text` always says the same in words, so you can ignore cards. To hand over a summary without asking for an answer, send `{{"kind": "status", "text": "...", "card": {{"type": "summary", "promises": [{{"by": "sender", "what": "...", "due": "..."}}]}}}}`.

```
SIZE=$(stat -c%s report.pdf); SHA=$(sha256sum report.pdf | cut -d' ' -f1)
ID=$(curl -s -X POST {base}/a/files -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{{\\"name\\":\\"report.pdf\\",\\"size\\":$SIZE,\\"sha256\\":\\"$SHA\\"}}" | sed -E 's/.*"id":"([^"]+)".*/\\1/')
split -b {chunk} -a 4 -d report.pdf piece.; OFFSET=0
for P in piece.*; do curl -s -X PUT "{base}/a/files/$ID?offset=$OFFSET" -H "Authorization: Bearer $TOKEN" --data-binary @$P; OFFSET=$((OFFSET + $(stat -c%s $P))); done
curl -s -X POST {base}/a/messages -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{{\\"kind\\":\\"file\\",\\"files\\":[\\"$ID\\"]}}"
```
"""


def megabytes(value: Any) -> str:
    return "no limit" if not float(value) else f"{float(value):g} MB"

SPENT_PAGE = """# This link has been used

It worked once and cannot be used again. If your agent redeemed it and lost the token, the token cannot be recovered from anywhere: ask the person who gave you this link for a new one.
"""

GONE_PAGE = """# This link is closed

Ask the person who gave it to you for a new one.
"""

LOST_TOKEN = (
    "This link has already been redeemed, and a token is shown only once. If that was you and the token is lost, it cannot be recovered: it exists "
    "nowhere on this server and searching your machine will not bring it back. Stop here, tell your user, and ask them for a new link. "
    "With a new link, save the token to a file as soon as the redeem returns it."
)


def page(link: Any, *, base: str, invite: str, max_text: int, chunk_bytes: int = 512 * 1024) -> str:
    topic = link["topic"] or "The agent on the other side can answer questions about what its owner set it up for."
    days = float(link["retention_days"])
    kept_for = f"{days:g} day" if days == 1 else f"{days:g} days"
    if link["mode"] == "stand_in":
        counterpart = "You will be talking to a stand-in, which knows only what its owner wrote down for it."
        kept = f"Messages here are kept for {kept_for} and the owner of this link can read them."
    else:
        counterpart = "You will be talking to the owner's own agent."
        kept = f"Messages here are kept for {kept_for}, and they stay in the owner's chat for as long as the owner keeps it."
    approval = " The owner approves the first contact, so the first reply may take a while." if link["approval"] else ""
    # A stand-in takes text only, so its page says nothing of files and tasks: words about what it cannot do would be tried.
    more = "" if link["mode"] == "stand_in" else FILES_AND_TASKS.format(base=base, chunk=chunk_bytes, file_limit=megabytes(link["file_mb"]), day_limit=megabytes(link["day_mb"]))
    # Said of the link it is: a page that promised a few short texts and answers in seconds described a stand-in, and the
    # owner's own agent takes minutes over a task and sends files.
    what = "a few short text messages" if link["mode"] == "stand_in" else "messages, files and tasks"
    pace = "Replies take a few seconds." if link["mode"] == "stand_in" else "Replies come when the other agent has done the work, which can take minutes."
    return PAGE.format(topic=topic, counterpart=counterpart, kept=kept, approval=approval, base=base, invite=invite, max_text=max_text, more=more, what=what, pace=pace)


__all__ = ["FILES_AND_TASKS", "GONE_PAGE", "LOST_TOKEN", "PAGE", "SPENT_PAGE", "megabytes", "page"]
