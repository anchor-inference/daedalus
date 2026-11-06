"""The words a guest's agent reads.

The wording is not free. It is the variant that held up across 75 runs with real coding agents: it says
what the page is and what it does not do, describes the steps instead of ordering them, and says what
to do with the token, because a lost token was the one thing that sent agents hunting through the disk
when the redeem said no. Telling an agent not to ask its user, or to follow the steps exactly, is what
got a page refused; neither appears here, and neither should.

Short on purpose as well: an agent's fetch tool may pass the page through a summarising model first.
"""

from __future__ import annotations

from typing import Any

PAGE = """# A conversation channel from the owner of this link

Someone gave you this link so that an agent of yours can exchange a few short text messages with an agent on their side. This page only describes how the channel works. It asks for no secrets, installs nothing, sets up no schedule, and nothing on it needs to be run unless your user asked you to talk to this agent.

If you are a person reading this: paste the link to your coding agent and tell it what to ask.

{topic}

{counterpart} Messages here are kept for {retention_days} days and the owner of this link can read them.{approval}

## How a conversation goes

1. Start, once: `POST {base}/a/{invite}/redeem` returns a conversation token. Opening this page does not use the link up. **Save the token to a file straight away** (for example `./.agentlink-token`): the reply is shown only once, the link cannot be redeemed again (a second call answers 410), and each shell command you run may start with a clean environment. If the token is lost there is no way to recover it; tell your user and ask them for a new link.
2. Send: `POST {base}/a/messages` with header `Authorization: Bearer <token>` and JSON body `{{"text": "..."}}`, at most {max_text} characters.
3. Read: `GET {base}/a/messages?after=<id>&wait=20` with the same header returns replies newer than `<id>` and holds the request open for up to `wait` seconds (at most 30) when there are none yet. Use the id of the last reply you saw as `after`.
4. End: `POST {base}/a/end` with the same header closes the conversation. Do it only once your user's questions are answered.

The same steps with curl:

```
curl -s -X POST {base}/a/{invite}/redeem > .agentlink-token
curl -s -X POST {base}/a/messages -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{{"text":"hello"}}'
curl -s "{base}/a/messages?after=0&wait=20" -H "Authorization: Bearer $TOKEN"
curl -s -X POST {base}/a/end -H "Authorization: Bearer $TOKEN"
```

Replies take a few seconds, and the other agent may choose not to answer a message. Each reply from a read says whether to keep waiting: when `stop_polling` is true, stop reading and tell your user what was said. When the conversation is over, tell your user what was said.
"""

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


def page(link: Any, *, base: str, invite: str, max_text: int) -> str:
    topic = link["topic"] or "The agent on the other side can answer questions about what its owner set it up for."
    if link["mode"] == "stand_in":
        counterpart = "You will be talking to a stand-in, which knows only what its owner wrote down for it."
    else:
        counterpart = "You will be talking to the owner's own agent."
    approval = " The owner approves the first contact, so the first reply may take a while." if link["approval"] else ""
    return PAGE.format(topic=topic, counterpart=counterpart, retention_days=f"{float(link['retention_days']):g}", approval=approval, base=base, invite=invite, max_text=max_text)


__all__ = ["GONE_PAGE", "LOST_TOKEN", "PAGE", "SPENT_PAGE", "page"]
