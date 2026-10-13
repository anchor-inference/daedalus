# Voice mode (beta)

`/app/voice` is a conversation, not a chat window. You talk; a small fast model — the *concierge* —
answers out loud in a second or two. It is not the agent that does the work: it is the manager who
stays on the line while the engineers work. Small talk, quick facts and "what is running?" it answers
itself. Anything substantial it hands to a real agent session with `Delegate` and says so at once
("one moment, I am setting that up"), so the conversation never stalls on a four-minute tool call.
Several things asked at once become several agents, running in parallel. While they work you can keep
talking: add an instruction to one that is already going, ask what came back, stop one. When an agent
finishes, the report arrives in the conversation and the concierge summarises it in a sentence.

The concierge is an ordinary session: its transcript is in the app under **Voice → Transcript**, its
history is compacted like any other, and its calls appear in Usage. What it is not is an agent — its
tools are `Delegate`, `Agents`, `AgentResult`, `StopAgent` and `WebSearch`, and nothing else. It has no
shell, no files and no workspace; the agents beside it have all of that. That split is enforced by the
host, not by the prompt, and no mode can widen it.

```toml
[voice]
enabled = true
preset = "openrouter.qwen-qwen3.7-flash"   # a preset from [presets]; pick a fast, no-thinking model
                                           # chosen in the app too: Settings -> Voice -> Model, which
                                           # lists the presets and warns about the slow ones. Empty
                                           # means the default model. A change applies to the next
                                           # thing you say, with no restart.

[voice.tts]                  # reading the answer out loud, in the order it is tried
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

**Hearing you.** Chrome, Edge and Safari recognise speech in the browser itself, streaming, with no
server involved — that is the primary path and it costs nothing. Firefox has no such API: there the
page records instead, cuts an utterance when you have been quiet for about a second, and posts it to
be transcribed by the `[asr]` endpoint (the same one that transcribes voice notes in the chat). With
neither, the page still works from the keyboard and says why the microphone is missing.

**Speaking back.** Three ways, tried in that order, and the first is the one to use.

**A voice that runs here.** **Settings → Voice → Voice (speech synthesis)** is a catalog of eighteen
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

**Limits.** It is beta and it shows. Recognition quality is the browser's, and it mishears names and
identifiers; barge-in cuts the audio but the concierge's turn keeps its own run until it settles; a
delegated agent that stops to ask a question is reported to you but is answered in its own session,
not by voice; reports arriving while no page is open are held and delivered together at the next
connect, so a long silence can start with a summary of several agents at once. On iOS, audio plays
only after the first tap on the page — take the mic once and it works for the session.

## Speech recognition in the chat is not voice mode

Dictating into the composer and the voice notes you send in the chat or in Telegram are turned into text
by the speech recognition set in **Settings → Voice & speech → Speech recognition for the chat**
([/app/settings/voice#asr.transcriber](/app/settings/voice#asr.transcriber)), the `[asr]` section: a
local model downloaded under the same section (no key, nothing leaves the machine), or an
OpenAI-compatible `/audio/transcriptions` endpoint. Nothing on the voice-mode page changes it. The
agent can switch it for you with `SettingsChange` — `{"asr": {"transcriber": "gigaam-ru"}}` for a
downloaded local model, `"cloud"` for the endpoint — once the model is installed.
