# The companion

`/app/companion` is your standing personal agent: one agent you talk to by text and by voice, which
keeps hold of what you care about between conversations and hands the real work to agent sessions. It
is not the agent that does the work — it is the manager who stays on the line while the engineers
work. Small talk, quick facts and "what is running?" it answers itself. Anything substantial it hands
to a real agent session with `Delegate` and says so at once, so the conversation never stalls on a
four-minute tool call. Several things asked at once become several agents, running in parallel. When
an agent finishes, the report arrives in the conversation and the companion summarises it.

The page has a header — the companion's face, its name, what it is doing, **Pause** and **Call** — and
seven tabs:

- **Conversation** — the transcript, a field to type in, and the agents it started beside it. Typed
  messages and spoken turns are one history. **Call** puts the orb in the middle and opens the
  microphone; answers are read aloud only on a call.
- **Goals** — what it owns over time (below). Add, pause, finish or delete one from here too.
- **Schedule** — its own schedules, the ones waiting for your approval, and its goals' cadences.
- **Activity** — what it did, in order: delegations, agents' results and questions, goal checks and reports.
- **Computer** — its agents' browsers, live, with **Take control** and **Give back**. The companion
  has no browser of its own.
- **Tools** — what it may call itself, and what its agents may use.
- **Memory** — the notes it keeps across conversations (`Remember`); forget one from here.

The face and the name are yours to choose (the cog in the header): a shape, a colour, eyes and an
accessory, drawn from four parameters wherever the companion is shown — the header, the Agents list,
and `GET /api/companion` for any other client.

## What it may do

The companion is an ordinary session: its transcript is in the app, its history is compacted like any
other, and its calls appear in Usage. Its tools are `Delegate`, `Agents`, `AgentResult`, `StopAgent`,
`Projects`, `WebSearch`, the goal tools (`GoalCreate`, `GoalUpdate`, `GoalList`, `GoalReport`),
`Remember`/`Recall`/`Forget`, its own schedules (`ScheduleCreate`, `ScheduleList`, `ScheduleDelete` —
every schedule waits for your approval), `Notify`, and reading the calendar (`CalendarEvents`,
`CalendarList`, `CalendarFindTime`). It has no shell, no files, no browser and no workspace; the agents
it starts have all of that, under the same rules and approvals as any session. That split is enforced
by the host, not by the prompt, and no mode can widen it.

## Goals

A goal is something the companion owns over days rather than one task: "keep the bakery's prices
current", "find me a flat under 80k". It has a title, a brief (what done looks like, what it may do on
its own), a status (`active`, `paused`, `done`, `dropped`), the agents working on it, its last report,
and optionally a cadence — `every 6h`, `30m`, `daily`, or a five-field cron in UTC. On the cadence the
host gives the companion a turn to check the goal: it looks at the goal's agents, delegates what needs
doing and ends with `GoalReport`. Checks are never more often than `goal_min_interval_minutes`.

## When nobody is listening

An agent's result that arrives with no page open (or while the companion is paused) is held and
delivered to the conversation when you come back, marked with how long ago it happened. A finished
result also goes to the Inbox and on to your phone at once, as your notification settings decide, and
so does a goal report made while nobody is listening; questions and approvals reach the Inbox as every
session's do.

**Pause** stops the companion acting on its own — no goal checks and no reports into the
conversation — until you resume it. It still answers when you talk to it.

## Two models

A spoken turn wants an answer in a second or two; a typed message, an agent's report and a goal check
want a good answer and nobody is waiting on them out loud. So there are two models:

```toml
[companion]
enabled = true
name = "Daedalus"
preset = "openrouter.qwen-qwen3.7-flash"   # live voice turns: pick a fast, no-thinking model. Empty
                                           # means the default model when that is fast enough.
background_preset = ""                     # typed messages, reports, goal checks. Empty = the default.
goal_min_interval_minutes = 15
progress = true                            # relay agents' words on the way, not only their results

[companion.avatar]
shape = "orb"          # orb | drop | square | star | cloud
color = "#7c6cf0"
eyes = "dots"          # dots | wide | sleepy | happy | visor
accessory = "none"     # none | antenna | halo | hat | glasses | bow

[companion.tts]              # reading the answer out loud, in the order it is tried
local_voice = ""             # a voice downloaded onto this machine: "ru-dmitri", "en-amy", ...
local_speaker = ""           # which voice inside a multi-voice model ("af_sarah", "expr-voice-4-f")
local_speed = 1.0            # 0.5 - 2.0
local_threads = 2
provider = ""                # else an endpoint: a provider id from [providers], its URL and key
url = ""                     # or an endpoint of its own, e.g. a local speech server
api_key = ""
model = "gpt-4o-mini-tts"
voice = "alloy"
format = "mp3"               # mp3 | opus | pcm
```

Both models are chosen in the app as well, under **Settings → Voice & speech**, and a change applies
to the next turn with no restart. An installation that had a `[voice]` section has it moved to
`[companion]` on the first start, every key as it was.

## From other clients

`GET /api/companion` answers `{session_id, name, avatar: {shape, color, eyes, accessory}, paused,
state, running_agents}`, where `state` is `idle`, `listening`, `thinking` or `working`.
`PATCH /api/companion` with `{paused}` pauses or resumes it and answers the same shape.
`POST /api/companion/message` with `{text, attachments?, source?}` is a text turn in the companion's
conversation; `source` is `app`, `quick_chat`, `voice` or `telegram`. A file can go two ways: staged
with `POST /api/companion/attachments` (a multipart `file`), whose answer carries the `id` the message
names, or uploaded into the companion's session with the ordinary `POST /api/sessions/{id}/upload`,
which is a text turn of the companion like a typed message.

## Talking to it

**Hearing you.** Chrome, Edge and Safari recognise speech in the browser itself, streaming, with no
server involved — that is the primary path and it costs nothing. Firefox has no such API: there the
page records instead, cuts an utterance when you have been quiet for about a second, and posts it to
be transcribed by the `[asr]` endpoint (the same one that transcribes voice notes in the chat). With
neither, the page still works from the keyboard and says why the microphone is missing.

**Speaking back.** Three ways, tried in that order, and the first is the one to use.

**A voice that runs here.** **Settings → Voice & speech → Voice (speech synthesis)** is a catalog of eighteen
voices — Supertonic at the top of it, Piper in ten languages including three Russian ones, and Kokoro
and KittenTTS for English — between thirteen and three hundred and fifty megabytes each. Pick one,
press **Play sample** to hear it say a sentence in its own language, and press **Use this one**. From
then on every answer is synthesised on this machine's processor: no endpoint, no key, nothing metered,
and it works with the network down. Most of them render four to five times faster than a person talks,
so the audio is ready before the sentence before it has finished playing; the one that does not —
Piper's "high" quality — says so on its own card. Numbers, dates and Latin words inside a Russian
sentence are read properly: each Piper archive carries its own copy of espeak-ng's data, so nothing
has to be installed for it.

The recommended Russian voice is **Supertonic 3**: a hundred and twenty-three megabytes that reads
thirty-one languages, renders at forty-four kilohertz — twice the rate of the rest — offers ten voices
inside one download, and is still quicker than any Piper voice here. A Russian answer going to it has
its stress marked first, from a small dictionary of the words an assistant says every day, because
that is the one thing about a Russian voice a listener notices immediately. Its licence is
OpenRAIL-M rather than a permissive one — commercial use is allowed with use restrictions that travel
with it — and the card says so; the Piper voices, one of them public domain, are still there for an
installation that would rather not think about it.

**An endpoint**, if you would rather: any OpenAI-compatible `/audio/speech` — a self-hosted
Kokoro-FastAPI or Piper server, or a hosted model like `gpt-4o-mini-tts`. Set `provider` to reuse a
configured provider's URL and key, or `url` and `api_key` for an endpoint of its own. A local voice
that fails is *not* quietly replaced by this one: you chose it, and a failure hidden behind a metered
fallback is a failure nobody fixes.

**The browser**, with neither configured: every modern browser has a synthesiser, nothing has to be
installed, and it sounds like it.

Whichever speaks, the answer is spoken a sentence at a time as it is written, so speech starts before
the model has finished the paragraph, and talking over it stops it.

**Limits.** Recognition quality is the browser's or the local model's, and it mishears names and
identifiers; barge-in cuts the audio but the companion's turn keeps its own run until it settles; a
delegated agent that stops to ask a question is reported to you, and your answer goes back to it
through the companion. On iOS, audio plays only after the first tap on the page — start the call once
and it works for the session.
