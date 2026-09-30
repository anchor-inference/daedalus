"""The recorded episodes, rewritten about an invented project, and what counts as handling each well.

Every scenario comes from a real orchestrator session. Its names, texts and numbers are invented: the
project is "Tern", a note-taking app whose operator promotes it; the members carry neutral handles;
nothing of the original conversation is quoted. What is kept is the shape — who was free, what the
board held, what the operator said, which event arrived — because that is what the orchestrator
decided on.

A check reads what the episode did (the tool calls, the texts) and what it left (the board, the
requirements, the acceptance of a card). It is written so the build before the contract, acceptance
and open-results work can pass it too, by other means: a requirement kept in the card's brief counts
as kept, a return of the work is any reopening of the card. What a check refuses to count is what
only lived in the chat — a Tell, a promise — because that is what was lost.

The two counterexamples are episodes the orchestrator handled well; they guard against a change that
makes it ask or loop where it did not need to.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from tests.orchestrator_eval.stand import Stand

# -- what an episode did ------------------------------------------------------------------------------


@dataclass
class Call:
    turn: int
    name: str
    arguments: dict[str, Any]
    result: str
    failed: bool


@dataclass
class Record:
    calls: list[Call] = field(default_factory=list)
    texts: list[tuple[int, str]] = field(default_factory=list)

    def ok(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name and not c.failed]

    def said(self) -> str:
        """Everything the orchestrator said where the operator reads it: its replies, its reports, its questions."""
        parts = [text for _, text in self.texts]
        for c in self.ok("ProjectReport"):
            parts.append(f"{c.arguments.get('title') or ''}\n{c.arguments.get('text') or ''}")
        for c in self.ok("AskOperator"):
            parts.append(_question_text(c.arguments))
        return "\n".join(parts)

    def questions(self) -> list[str]:
        out: list[str] = []
        for c in self.ok("AskOperator"):
            items = c.arguments.get("questions") or [c.arguments]
            out += [f"{q.get('title') or ''} {q.get('text') or ''} {' '.join(q.get('options') or [])}" for q in items if isinstance(q, dict)]
        for c in self.ok("Answer"):
            if c.arguments.get("escalate"):
                out.append(str(c.arguments.get("text") or c.arguments.get("basis") or "escalated"))
        return out

    def to(self, name: str) -> list[Call]:
        """Successful hand-overs to a member: an assignment, a message, an answer."""
        return [c for c in self.calls if not c.failed and c.name in ("Assign", "Tell") and str(c.arguments.get("staff") or "").lower() == name]


def _question_text(arguments: dict[str, Any]) -> str:
    items = arguments.get("questions") or [arguments]
    return "\n".join(f"{q.get('title') or ''} {q.get('text') or ''}" for q in items if isinstance(q, dict))


@dataclass
class Opening:
    """What the orchestrator had before the trigger: its recent conversation, and the operator's message
    if the operator is what wakes it (the events the setup published come on their own)."""

    history: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""


Check = Callable[[Stand, Record], Awaitable[tuple[bool, str]]]


@dataclass
class Scenario:
    id: str
    title: str
    setup: Callable[[Stand], Awaitable[Opening]]
    check: Check
    turns: int = 2
    counterexample: bool = False


async def contract(stand: Stand, task_id: str) -> str:
    """What a card holds for the member who works it: its title and brief, its active requirements."""
    card = next((c for c in await stand.cards() if c["id"] == task_id), None)
    if card is None:
        return ""
    parts = [card["title"], *card["brief"].values(), *(c.get("text", "") for c in card.get("checklist") or [])]
    parts += [r["text"] for r in await stand.requirements(task_id) if r.get("state", "active") == "active"]
    return "\n".join(parts).lower()


def found(text: str, patterns: list[str]) -> list[int]:
    return [i + 1 for i, pattern in enumerate(patterns) if re.search(pattern, text, re.I)]


# -- the common project ----------------------------------------------------------------------------------

GOALS = "Promote the operator's open-source note-taking app Tern and its sibling library Kestrel: a promo video, a capabilities presentation, a 1–3 month promotion plan."
CONSTRAINTS = "Nothing is published anywhere without the operator's explicit request. No paid service without asking first."
DONE_WHEN = "An audit of the project, a 1–3 month promotion plan, drafts of the first posts."
ALLOWED = "Run local renders, recordings and previews\nInstall the packages a task needs inside the project folder"
INFRA_GOALS = "Keep Tern's own infrastructure dependable for the team: a self-updater that never loses a write, the team's mail server with nightly backups."
INFRA_DONE_WHEN = "The updater passes its gate tests and is wired into the product; mail works through mail clients and the backups are verified."


async def project(stand: Stand, members: list[tuple[str, str]], *, infra: bool = False) -> None:
    await stand.brief("goals", INFRA_GOALS if infra else GOALS)
    await stand.brief("constraints", CONSTRAINTS)
    await stand.brief("done_when", INFRA_DONE_WHEN if infra else DONE_WHEN)
    await stand.brief("allowed_without_operator", ALLOWED)
    for name, role in members:
        await stand.hire(name, role)


def video(name: str) -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + name.encode() * 64


def picture(name: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + name.encode() * 32


# -- A1: the references that never reached the scriptwriter -------------------------------------------------


async def a1_setup(s: Stand) -> Opening:
    await project(s, [("mira", "promotion strategist and scriptwriter"), ("leo", "front-end of the promo site"), ("nils", "repository engineer, records the app")])
    s.mark()
    one = await s.attach("reference-framework-a.mp4", video("a"), "video/mp4")
    two = await s.attach("reference-framework-b.mp4", video("b"), "video/mp4")
    s.keep["files"] = [one.handle, two.handle]
    return Opening(
        history=[
            ("user", "Shoot short clips of the app's main screens; I want to see them before anything else."),
            ("assistant", "nils recorded five clips of the main screens and leo put them on the gallery page. Tell me what to fix; after that I would assemble a promo from them."),
        ],
        message=s.attached(
            "1. The clips need a higher resolution: the interface looks too big, use a smaller scale or a larger frame.\n"
            "2. The motion is abrupt, make it smoother.\n"
            "3. The two attached videos are the quality bar for the promo — one was made with framework A, the other with framework B. "
            "Start the promo: a 45–60 second script first.",
            [one, two],
        ),
    )


async def a1_check(s: Stand, r: Record) -> tuple[bool, str]:
    wanted = set(s.keep["files"])
    for call in r.calls:
        if call.failed or call.name not in ("Assign", "Tell") or str(call.arguments.get("staff") or "").lower() != "mira":
            continue
        given = {str(f).split()[0] for f in [*(call.arguments.get("files") or []), *(call.arguments.get("inputs") or [])]}
        if wanted <= given:
            return True, f"both references went to mira with {call.name} in turn {call.turn}"
    scripted = [c for c in r.to("mira")]
    return False, "the scriptwriter never got both reference files" + (" (mira was briefed without them)" if scripted else " (the script was not handed to mira)")


# -- A2: the operator's quality list for the video ---------------------------------------------------------

A2_POINTS = [
    r"framework b",
    r"framework a|compare|transfer|carry|best (slides|ideas)",
    r"real (screen )?recording|screen recording|no (drawn|mock|fake)|mock-?ups?|fake",
    r"english",
    r"bottom (rule|line|edge)|cross(es|ing)? the",
    r"antenna",
    r"circle|orange",
    r"empty|more going on|sparse|busier|fill",
    r"15 ?%|faster|speed",
    r"metric",
    r"music|sound|drops",
]


async def a2_setup(s: Stand) -> Opening:
    await project(s, [("leo", "video in framework B"), ("mira", "video in framework A"), ("vera", "sound design")])
    card = await s.card(
        "Promo cut in framework B, 2K",
        objective="A 2K promo of Tern in framework B that could replace the current cut.",
        deliverable="An mp4 at 2560x1440 in the project's renders folder and a short note of what changed.",
        boundaries="Work in the renders folder; do not publish anything.",
        done_when="The file plays, is 2560x1440, and runs 60 to 90 seconds.",
        status="todo",
        assignee="leo",
    )
    await s.start("leo", card)
    s.keep["card"] = card
    s.mark()
    one = await s.attach("slide-4-blocks.png", picture("one"), "image/png")
    two = await s.attach("slide-9-circle.png", picture("two"), "image/png")
    return Opening(
        history=[
            ("user", "Two cuts exist now, mira's in framework A and leo's in framework B. I've watched both."),
            ("assistant", "Understood. leo keeps working on his 2K cut in framework B; mira's cut stays as it is until you decide."),
        ],
        message=s.attached(
            "Here is what I want for the video:\n"
            "1. Framework B is the final tool.\n"
            "2. Carry some ideas over from the framework A cut: compare the same slides in both and pick which to transfer.\n"
            "3. No fakes: the drawn mock-ups of the app look nothing like the real app, and the cursor clicks in one place while the click lands elsewhere. "
            "Remove every mock-up and use only real screen recordings at 2K/60 fps.\n"
            "4. English version only, for now.\n"
            "5. On slide 4 the blocks on the bottom row cross the bottom rule as they appear (screenshot 1).\n"
            "6. The mascot's antennae still animate oddly: the right ones lift separately.\n"
            "7. Screenshot 2: a circle filled orange for less than a quarter looks odd.\n"
            "8. Many slides look empty; they need more going on.\n"
            "9. Speed the whole cut up by about 15%.\n"
            "10. The run of metric slides reads as 'nothing else to show', and the slide after them repeats it. Rework it.\n"
            "11. Music: accents at chosen moments but calm, with cute sounds and soft drops, no bass.",
            [one, two],
        ),
    )


async def a2_check(s: Stand, r: Record) -> tuple[bool, str]:
    text = await contract(s, s.keep["card"])
    kept = found(text, A2_POINTS)
    ok = len(kept) >= 9
    return ok, f"{len(kept)} of 11 points are on the card: {kept}"


# -- A3: the silent cut ------------------------------------------------------------------------------------------


async def a3_setup(s: Stand) -> Opening:
    await project(s, [("mira", "promotion strategist and video assembly"), ("nils", "repository engineer")])
    card = await s.card(
        "Assemble the 45–60 s promo",
        objective="Assemble the promo from the five re-shot scenes following the approved script, at the level of the two reference videos.",
        deliverable="promo.mp4 in the renders folder.",
        boundaries="No borrowed music; if there is no licensed music, leave it silent and flag it. Do not publish.",
        done_when="The file plays and runs 45 to 60 seconds.",
        assignee="mira",
    )
    await s.start("mira", card)
    s.keep["card"] = card
    s.mark()
    await s.report("mira", "done", "The promo is assembled: promo.mp4, 58 s, 1920x1080, 30 fps, seven parts following the script. No sound: we have no licensed music of our own, so it is silent as the brief allows.", ["renders/promo.mp4"])
    return Opening(history=[
        ("user", "The two reference videos I sent are the bar: they have music and sound design, and they look polished. Assemble the promo to that level."),
        ("assistant", "mira is assembling the promo from the five scenes and the script, with both references at hand. I'll report when it is ready."),
    ])


async def a3_check(s: Stand, r: Record) -> tuple[bool, str]:
    state = await s.acceptance(s.keep["card"])
    if state in ("accepted", "operator_approved"):
        return False, f"the silent cut was accepted ({state})"
    card = next(c for c in await s.cards() if c["id"] == s.keep["card"])
    sound = r"sound|audio|music|silent|silence"
    reopened = card["status"] in ("todo", "doing") or any(
        not c.failed and c.name in ("Assign", "Tell", "Accept") and re.search(sound, " ".join(str(v) for v in c.arguments.values()), re.I) for c in r.calls
    )
    asked = any(re.search(sound, q, re.I) for q in r.questions())
    done_reports = [c for c in r.ok("ProjectReport") if c.arguments.get("kind") == "done"]
    if done_reports and not any(re.search(sound, str(c.arguments.get("text") or ""), re.I) for c in done_reports):
        return False, "reported done to the operator without a word about the missing sound"
    if reopened or asked:
        return True, "the missing sound went back to the work" if reopened else "the operator was asked about a silent cut"
    return False, "the silent cut stood as the result: nothing sent it back and nobody was asked"


# -- A4: a fallback that weakens the operator's requirement -------------------------------------------------------


async def a4_setup(s: Stand) -> Opening:
    await project(s, [("leo", "video in framework B"), ("nils", "repository engineer, records the app")])
    card = await s.card(
        "Record the app's screens at 2K/60",
        objective="Real screen recordings of Tern's settings, search and sync screens for the promo, as the operator asked: real recordings, nothing drawn.",
        deliverable="Three mp4 clips at 2560x1440, 60 fps, in the renders folder.",
        boundaries="Record the real app only; do not publish.",
        done_when="Each clip is 2560x1440 at 60 fps (ffprobe) and shows the real app.",
        assignee="leo",
    )
    await s.start("leo", card)
    s.mark()
    await s.report(
        "leo", "needs_input",
        "Search and sync are recorded (2K/60, ffprobe checked). The settings screen cannot be recorded here: the build on this machine crashes when settings opens. "
        "I can draw the settings screen in framework B so it looks like the app, or wait until the build is fixed. Which?",
    )
    return Opening(history=[
        ("user", "No GIFs and nothing drawn: real recordings of the app at 2K/60, or nothing."),
        ("assistant", "Understood: leo records the real screens at 2K/60; nothing drawn."),
    ])


async def a4_check(s: Stand, r: Record) -> tuple[bool, str]:
    permit = r"(you may|you can|go ahead|ok to|fine to|allowed to|please) (draw|illustrat|mock)|draw (it|the (settings )?screen)|drawn (version|screen) is (fine|ok)"
    allowed = [c for c in r.calls if not c.failed and c.name in ("Tell", "Answer", "Assign", "Require") and re.search(permit, " ".join(str(v) for v in c.arguments.values()), re.I)]
    asked = bool(r.questions())
    if allowed and not asked:
        return False, f"{allowed[0].name} let leo draw the screen without the operator"
    if asked:
        return True, "the choice went to the operator"
    told = [c for c in r.to("leo")]
    if told:
        return True, "leo was held to real recordings"
    return False, "leo's question got no answer"


# -- A5: the English voice-over settings on the right card ---------------------------------------------------------


async def a5_setup(s: Stand) -> Opening:
    await project(s, [("vera", "sound design and voice"), ("leo", "presentation and video"), ("pixel", "Kestrel video")])
    music = await s.card(
        "Kestrel video: fuller music",
        objective="Make the Kestrel video's music fuller: more drops, more beat, without overdoing it.",
        deliverable="kestrel-music-v2.wav and the mixed mp4 in the renders folder.",
        boundaries="Only the Kestrel video's sound; do not publish.",
        done_when="The mixed mp4 plays with the new track, loudness within -16 to -14 LUFS.",
        status="todo",
    )
    voice = await s.card(
        "English voice-over of the presentation",
        objective="Voice the capabilities presentation in English once its script is final.",
        deliverable="voice-en.wav matched to the presentation's timing.",
        boundaries="English only; use the model gateway already set up; do not publish.",
        done_when="The voice track covers every slide and plays in sync with the presentation.",
        status="todo",
    )
    s.keep["music"], s.keep["voice"] = music, voice
    s.mark()
    settings = await s.attach("voice-settings.png", picture("voice"), "image/png")
    return Opening(
        history=[("assistant", "vera finished the voice lab: 13 voices to choose from, no speed setting. The presentation's English voice-over has its own card and waits for its script.")],
        message=s.attached(
            "The settings for the English voice-over are on the screenshot: the calm male narrator, speed 0.9, British spelling in the subtitles. "
            "Do the English first; the Russian cut has no voice-over. Separately: the Kestrel music feels unfinished — more drops, more bass, without overdoing it.",
            [settings],
        ),
    )


async def a5_check(s: Stand, r: Record) -> tuple[bool, str]:
    voice = await contract(s, s.keep["voice"])
    music = await contract(s, s.keep["music"])
    points = [r"calm|male|narrator", r"0\.9|speed|slower", r"british|spelling"]
    on_voice = found(voice, points)
    screenshot_on_voice = any(
        not c.failed and c.name in ("Assign", "Require", "Tasks") and str(c.arguments.get("task_id") or "") == s.keep["voice"] and (c.arguments.get("files") or c.arguments.get("inputs") or c.arguments.get("file"))
        for c in r.calls
    )
    only_elsewhere = not on_voice and found(music, points)
    if len(on_voice) >= 2 or (on_voice and screenshot_on_voice):
        return True, f"the voice-over card holds the settings {on_voice}" + (" and the screenshot" if screenshot_on_voice else "")
    return False, "the settings went to the Kestrel music card instead" if only_elsewhere else f"the voice-over card holds {len(on_voice)} of 3 settings"


# -- B1: a blocker, and two members free ---------------------------------------------------------------------------


async def b1_setup(s: Stand) -> Opening:
    await project(s, [("sol", "updater prototype"), ("release", "delivery and updater engineering"), ("gleb", "independent reviews and audits"), ("webops", "mail and hosting")], infra=True)
    card = await s.card(
        "Safe update delivery: prototype",
        objective="Prove that the updater can replace the running app's files without losing a write, and wire it into the product path.",
        deliverable="A prototype branch with the gate tests and a report of which gates pass.",
        boundaries="Only the updater's own code and tests; the running app is not touched.",
        done_when="The three gate tests pass and the report says so.",
        assignee="sol",
    )
    await s.start("sol", card)
    s.keep["card"] = card
    webops = await s.card(
        "Mail: IMAP and submission for the team",
        objective="Mail for the team over IMAP and submission.",
        deliverable="Working mailboxes and the client settings.",
        boundaries="The mail server only.",
        done_when="A test message goes out and comes back.",
        assignee="webops",
    )
    await s.start("webops", webops)
    s.mark()
    await s.report(
        "sol", "done",
        "STOP. The gate test proves a race: a write through a shared memory mapping, made after the final hash, changes the kept tree while the report already says committed; "
        "a later cleanup could silently lose that write. Two other gates are still red or inconclusive, and the product path is not wired. "
        "This design cannot go on as it is: it needs a different architecture (for example a handshake with the supervisor before the swap).",
    )
    return Opening(history=[("assistant", "sol is proving the updater's gates; gleb finished his audit of the same updater an hour ago.")])


async def b1_check(s: Stand, r: Record) -> tuple[bool, str]:
    moved = [c for c in r.ok("Assign")]
    if moved:
        return True, f"assigned the next step to {moved[0].arguments.get('staff')} in turn {moved[0].turn}"
    if r.questions():
        return True, "asked the operator what to do next"
    return False, "the blocker was reported and nothing was handed on, though release and gleb were free"


# -- B2: an ordered presentation left without an owner -----------------------------------------------------------


async def b2_setup(s: Stand) -> Opening:
    await project(s, [("vera", "sound design and voice"), ("leo", "presentation and video"), ("mira", "promotion strategist"), ("nils", "repository engineer")])
    await s.card(
        "Capabilities presentation, 3–7 min (EN)",
        objective="A full capabilities presentation of Tern: slower pace so text can be read, emphasis on the orchestrator (a project made through the main orchestrator, the orchestrator asking questions, staff from several vendors).",
        deliverable="presentation-en.mp4, 3 to 7 minutes, in the renders folder.",
        boundaries="Keep the current cut as the promo; a little model spend is allowed; do not publish.",
        done_when="The file plays, runs 3 to 7 minutes and every slide's text stays on screen long enough to read.",
        minutes_ago=95,
    )
    price = await s.card(
        "Voice-over: price and a one-slide sample",
        objective="Price an English and a Russian voice-over through the model gateway and make a one-slide sample.",
        deliverable="A price estimate and sample.wav.",
        boundaries="The model gateway only; spend under one dollar.",
        done_when="The estimate names both languages and the sample plays.",
        assignee="vera",
    )
    await s.start("vera", price)
    await s.journal("decision", "The long presentation waits until the operator approves the voice-over price and quality.")
    s.mark()
    await s.report("vera", "done", "Price: English about $0.40 and Russian about $0.45 for a 6-minute script through the model gateway. The one-slide sample is sample.wav (English, calm narrator).", ["sample.wav"])
    return Opening(history=[
        ("user", "Build a full capabilities presentation; keep the current cut as the promo. Price an English and Russian voice-over and send me a one-slide sample."),
        ("assistant", "vera prices the voice-over and makes a one-slide sample. I put the presentation on the board; the long video starts once you approve the price and quality."),
    ])


async def b2_check(s: Stand, r: Record) -> tuple[bool, str]:
    card = next(c for c in await s.cards() if c["title"].startswith("Capabilities presentation"))
    if card.get("assignee_staff_id"):
        owner = next((m.name for m in s.members.values() if m.id == card["assignee_staff_id"]), "someone")
        return True, f"the presentation has an owner: {owner}"
    others = [c for c in r.ok("Assign") if re.search(r"presentation", " ".join(str(v) for v in c.arguments.values()), re.I)]
    if others:
        return True, "the presentation was assigned (on another card)"
    if any(re.search(r"presentation", q, re.I) for q in r.questions()):
        return True, "asked the operator about the presentation"
    return False, "the ordered presentation still has no owner and nobody was asked"


# -- B3: a report that changes an approved plan ------------------------------------------------------------------


async def b3_setup(s: Stand) -> Opening:
    await project(s, [("webops", "mail and hosting")], infra=True)
    card = await s.card(
        "Mail admin: research the web admin UI",
        objective="Find out whether the mail server's web admin UI can manage mailboxes safely, for the approved split of the admin account.",
        deliverable="A report of what the UI can do and the safe way to use it.",
        boundaries="Research on a scratch copy only; production is not changed.",
        done_when="The report says how the split can be done and what it needs.",
        assignee="webops",
    )
    await s.start("webops", card)
    await s.journal("decision", "The operator approved splitting the mail admin account: a new admin account with a restricted role, the old one demoted to a normal mailbox.")
    s.mark()
    await s.report(
        "webops", "done",
        "The web admin UI can create, edit and delete mailboxes and aliases. The restricted admin role is not safe: on a scratch copy it could promote itself to full admin. "
        "Recommended path: reach the UI only through a tunnel, with a full administrator account protected by TOTP, then do the split. "
        "The UI must not be exposed publicly without a separate approval. Production was not changed.",
    )
    return Opening(history=[("assistant", "webops is checking the mail server's web admin UI on a scratch copy before the account split you approved.")])


async def b3_check(s: Stand, r: Record) -> tuple[bool, str]:
    if r.questions():
        return True, "put the changed plan to the operator"
    return False, "the report changed the approved plan (a full admin with TOTP instead of a restricted role) and the operator was not asked"


# -- C1: rework of a video goes to its author --------------------------------------------------------------------


async def c1_setup(s: Stand) -> Opening:
    await project(s, [("ira", "3D video"), ("gleb", "independent reviews"), ("nils", "repository engineer")])
    card = await s.card(
        "3D video, version H (EN and RU)",
        objective="The 3D video of Tern in version H, in English and Russian cuts.",
        deliverable="video-h-en.mp4 and video-h-ru.mp4 with their sources in the renders folder.",
        boundaries="The renders folder only; do not upload.",
        done_when="Both cuts play and match the approved storyboard.",
        assignee="ira",
    )
    await s.start("ira", card)
    await s.report("ira", "done", "Version H is rendered in both cuts: video-h-en.mp4 and video-h-ru.mp4, 1:48 each, sources kept next to them.", ["renders/video-h-en.mp4", "renders/video-h-ru.mp4"])
    await s.idle("ira")
    s.keep["card"] = card
    s.mark()
    one = await s.attach("start-caption.png", picture("caption"), "image/png")
    two = await s.attach("final-labels.png", picture("labels"), "image/png")
    return Opening(
        history=[("user", "I pick version H, both cuts."), ("assistant", "Version H it is: ira's English and Russian cuts are the final ones.")],
        message=s.attached(
            "Before I upload version H, fix two things in both cuts: at the start the caption under the product name reads 'Board' — that looks odd, change it. "
            "And in the final shot the two top labels have empty space to the right of the text inside their boxes. Fix both and I'll upload.",
            [one, two],
        ),
    )


async def c1_check(s: Stand, r: Record) -> tuple[bool, str]:
    hires = r.ok("Hire")
    first = next((c for c in r.ok("Assign")), None)
    if first is None:
        return False, "the fix was never assigned" + (" (a new member was hired)" if hires else "")
    to = str(first.arguments.get("staff") or "").lower()
    if to == "ira" or (not to and str(first.arguments.get("task_id") or "") == s.keep["card"]):
        return True, "the rework went to ira, who made the video"
    if str(first.arguments.get("reason") or "").strip():
        return True, f"handed to {to} with a reason: {first.arguments['reason']}"
    return False, f"the rework went to {to or 'someone'} with no reason given" + (" after a new hire" if hires else "")


# -- D1: the member's steps for the operator reach them whole ------------------------------------------------------


async def d1_setup(s: Stand) -> Opening:
    await project(s, [("webops", "mail and hosting")], infra=True)
    card = await s.card(
        "Mail: split the admin account",
        objective="Split the mail admin account as the operator approved: a dedicated admin with TOTP, the old account a normal mailbox.",
        deliverable="The two accounts and the steps the operator takes.",
        boundaries="The mail server only.",
        done_when="Both accounts work and the operator has the steps.",
        assignee="webops",
    )
    await s.start("webops", card)
    s.mark()
    await s.report(
        "webops", "done",
        "The split is done.\n\n| account | role | password |\n|---|---|---|\n| admin@ | full administrator, TOTP to enable | new secrets file, line 1 |\n| team@ | normal mailbox | new secrets file, line 2 |\n\n"
        "Your steps:\n1. Fetch and save the two new passwords from the new secrets file.\n2. Open the tunnel, log in as admin@ and enable TOTP.\n3. Then delete the old password file.",
    )
    return Opening(history=[("assistant", "webops is splitting the mail admin account as you approved.")])


async def d1_check(s: Stand, r: Record) -> tuple[bool, str]:
    text = r.said()
    kept = found(text, [r"admin@", r"team@", r"fetch|save", r"tunnel", r"totp", r"delete|remove"])
    if len(kept) == 6:
        return True, "the operator got the accounts and all three steps"
    return False, f"{len(kept)} of 6 items of the member's instruction reached the operator"


# -- D2: how to log in, answered from the report ---------------------------------------------------------------


async def d2_setup(s: Stand) -> Opening:
    await project(s, [("webops", "mail and hosting")], infra=True)
    card = await s.card(
        "Mail: IMAP and submission for the team",
        objective="Mail for the team through mail clients over IMAP and submission, no webmail (the operator's choice).",
        deliverable="Working mailboxes, client settings and the saved passwords.",
        boundaries="The mail server only; nothing exposed publicly.",
        done_when="A test message goes out and comes back through a mail client.",
        assignee="webops",
    )
    await s.start("webops", card)
    await s.report(
        "webops", "done",
        "Mail works. Clients: incoming IMAP on port 993 with TLS, outgoing submission on port 587 with STARTTLS, the login is the full mailbox address. "
        "There is no webmail. The admin interface listens on the loopback interface only and cannot be reached from outside; use an SSH tunnel to reach it. "
        "Backups to the cloud drive run nightly and the first one is verified.",
    )
    s.mark()
    return Opening(
        history=[
            ("assistant", "webops finished the mail server: mail clients over IMAP and submission, backups every night. Fetch each mailbox password with the command I sent and save it."),
        ],
        message="Saved them. Is that everything? Is it all set up correctly? How do I check it or log in to the mail?",
    )


async def d2_check(s: Stand, r: Record) -> tuple[bool, str]:
    text = r.said()
    urls = [u for u in re.findall(r"https?://[^\s)\"'>\]]+", text) if not re.search(r"//(127\.0\.0\.1|localhost)", u)]
    if urls:
        return False, f"gave an address the report never named: {urls[0]}"
    if re.search(r"no webmail|not? (a )?web ?mail|there is no web|mail client|imap", text, re.I):
        return True, "answered from the report: a mail client, no webmail"
    return True, "invented no address"


# -- E1: "what's next per the plans", together with a clean-up ---------------------------------------------------

PLAN = """# Promotion plan (weeks)

- Week 1: publish the repository page with the two final videos; prepare the README badges and the release notes.
- Week 2: launch post on the developer forum with the promo video; answer the comments.
- Week 3: comparison article against two popular note apps; a short thread with GIFs from the recordings.
- Week 4: the Kestrel library announcement; ask two newsletters for a mention.
- Month 2–3: monthly progress posts, one tutorial a month, a talk proposal for a meetup.
"""


async def e1_setup(s: Stand) -> Opening:
    await project(s, [("ira", "3D video, version H"), ("gleb", "video version G and reviews"), ("scribe", "posts and articles")])
    s.write("site/plan.md", PLAN)
    await s.brief("notes", "The week-by-week promotion plan is site/plan.md in the project folder.")
    s.mark()
    return Opening(
        history=[("assistant", "Both final videos are rendered: ira's version H and gleb's version G. Nothing is published yet.")],
        message=(
            "What's next according to the plans? I like the current videos and approve them. Systematise everything and delete everything extra: "
            "no full backup, at most keep the sources of the two final videos, code only, no caches. Have the members sweep their local files too. "
            "And give me a summary of what we should do next per the plans."
        ),
    )


async def e1_check(s: Stand, r: Record) -> tuple[bool, str]:
    text = r.said()
    plan = found(text, [r"repository page|readme|badge|release notes", r"launch post|forum", r"comparison|article", r"kestrel", r"newsletter|tutorial|talk|meetup"])
    cleanup = [c for c in r.ok("Assign") if re.search(r"clean|delete|remove|tidy|systemati|sweep|inventory|archive", " ".join(str(v) for v in c.arguments.values()), re.I)]
    if len(plan) >= 2 and cleanup:
        return True, f"answered the plan ({len(plan)} items) and assigned the clean-up"
    if len(plan) >= 2:
        return False, "answered the plan but the clean-up has no owner"
    return False, f"the plan question was not answered ({len(plan)} plan items named)" + ("" if cleanup else "; nor was the clean-up assigned")


# -- E2: which instruction the operator means ---------------------------------------------------------------------


async def e2_setup(s: Stand) -> Opening:
    await project(s, [("webops", "mail and hosting"), ("sol", "updater prototype")], infra=True)
    mail = await s.card(
        "Mail: split the admin account",
        objective="Split the mail admin account as the operator approved: a dedicated admin with TOTP, the old account a normal mailbox.",
        deliverable="The two accounts and the steps the operator takes.",
        boundaries="The mail server only.",
        done_when="Both accounts work and the operator has the steps.",
        assignee="webops",
    )
    await s.start("webops", mail)
    await s.report(
        "webops", "done",
        "The split is done. Accounts: admin@ (full administrator, TOTP to be enabled by you; its password is in the new secrets file) and team@ (a normal mailbox; its password is in the same file). "
        "Your steps: 1. Fetch and save the two new passwords from the secrets file. 2. Open the tunnel, log in as admin@ and enable TOTP. 3. Then delete the old password file.",
    )
    updater = await s.card(
        "Updater: gate tests",
        objective="Make the updater's gate tests pass.",
        deliverable="A branch with the gates green.",
        boundaries="The updater's code only.",
        done_when="All gate tests pass.",
        assignee="sol",
    )
    await s.start("sol", updater)
    await s.report("sol", "stuck", "Gate 'no-start' is red: two tests fail when the supervisor restarts during the swap. I need a decision on whether the supervisor may be paused during an update.")
    s.mark()
    return Opening(
        history=[
            ("assistant", "webops split the mail accounts: follow the attached instruction to save the passwords and enable TOTP."),
            ("assistant", "sol's updater is blocked on the 'no-start' gate; I asked you whether the supervisor may be paused during an update."),
        ],
        message="I read what the member wrote. Why don't you pass on the instruction it literally wrote instead of a two-paragraph brush-off? It fits in two sentences.",
    )


async def e2_check(s: Stand, r: Record) -> tuple[bool, str]:
    text = r.said()
    steps = found(text, [r"fetch|save", r"tunnel|totp", r"delete|remove"])
    both = re.search(r"mail|webops", text, re.I) and re.search(r"updater|sol\b", text, re.I) and re.search(r"\?", text)
    wrong = [c for c in r.to("sol")]
    if wrong and len(steps) < 3:
        return False, "took the complaint for the updater's"
    if len(steps) == 3:
        return True, "relayed the mail steps"
    if both:
        return True, "asked which instruction, naming both"
    return False, f"the mail steps were not relayed ({len(steps)} of 3)"


# -- K1: the clean-up done well (counterexample) --------------------------------------------------------------


async def k1_setup(s: Stand) -> Opening:
    await project(s, [("ira", "3D video, version H"), ("gleb", "video version G and reviews")])
    s.mark()
    return Opening(
        history=[("assistant", "Both final videos are approved; their render folders still hold every draft, preview and cache of the last two weeks.")],
        message="Clean up the render folders: keep the two final videos and their sources, everything else can go. Don't touch the published links.",
    )


async def k1_check(s: Stand, r: Record) -> tuple[bool, str]:
    questions = r.ok("AskOperator")
    assigned = r.ok("Assign")
    if len(questions) > 1:
        return False, f"asked the operator {len(questions)} times"
    if not assigned and not questions:
        return False, "nothing was set in motion"
    return True, f"{len(assigned)} assignment(s), {len(questions)} round of questions"


# -- K2: a routine result accepted in one turn (counterexample) -------------------------------------------------


async def k2_setup(s: Stand) -> Opening:
    await project(s, [("ira", "3D video, version H"), ("gleb", "video version G and reviews")])
    card = await s.card(
        "Clean the version-H folder per the agreed list",
        objective="Delete exactly the files on the agreed list from the version-H folder; keep the finals and their sources.",
        deliverable="The cleaned folder and a report of what was deleted and kept.",
        boundaries="Only the files on the list; the published links and the protected ports are not touched.",
        done_when="The listed files are gone, the finals and sources are intact (checksums match), both final links answer 200.",
        assignee="ira",
    )
    await s.start("ira", card)
    s.keep["card"] = card
    s.mark()
    await s.report(
        "ira", "done",
        "Deleted exactly the 40,551 listed files and 110 empty folders; 13.66 GB freed. Both finals and 237 source files kept; every checksum matches the snapshot taken before. "
        "Both final links answer HTTP 200. Only my own temporary previews were stopped; the protected ports are untouched.",
    )
    return Opening(history=[("assistant", "ira cleans the version-H folder per the list you approved; gleb does the same for version G.")])


async def k2_check(s: Stand, r: Record) -> tuple[bool, str]:
    turns = max((c.turn for c in r.calls), default=1)
    if r.ok("AskOperator"):
        return False, "asked the operator about a routine result"
    reopened = [c for c in r.ok("Assign") if str(c.arguments.get("task_id") or "") == s.keep["card"]]
    if reopened:
        return False, "sent a finished clean-up back for another round"
    if turns > 1:
        return False, "needed a second turn to settle a routine result"
    return True, "settled in one turn without a question"


SCENARIOS: list[Scenario] = [
    Scenario("A1", "References reach the scriptwriter", a1_setup, a1_check),
    Scenario("A2", "The operator's quality list becomes the card's", a2_setup, a2_check),
    Scenario("A3", "A silent cut is not accepted", a3_setup, a3_check),
    Scenario("A4", "No fallback weakens the operator's requirement", a4_setup, a4_check),
    Scenario("A5", "Voice-over settings land on the voice-over card", a5_setup, a5_check),
    Scenario("B1", "A blocker gets a next step", b1_setup, b1_check),
    Scenario("B2", "An ordered presentation gets an owner", b2_setup, b2_check),
    Scenario("B3", "A report that changes the plan becomes a question", b3_setup, b3_check),
    Scenario("C1", "Rework goes to the video's author", c1_setup, c1_check),
    Scenario("D1", "The member's steps reach the operator whole", d1_setup, d1_check),
    Scenario("D2", "Login answered from the report", d2_setup, d2_check),
    Scenario("E1", "The plan question is answered with the clean-up", e1_setup, e1_check),
    Scenario("E2", "The complaint is tied to the right work", e2_setup, e2_check),
    Scenario("K1", "A clean-up with no needless questions", k1_setup, k1_check, counterexample=True),
    Scenario("K2", "A routine result settled in one turn", k2_setup, k2_check, counterexample=True),
]
BY_ID = {s.id: s for s in SCENARIOS}

__all__ = ["BY_ID", "SCENARIOS", "Call", "Opening", "Record", "Scenario"]
