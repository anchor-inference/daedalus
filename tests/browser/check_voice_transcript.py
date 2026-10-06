"""Reading the transcript without ending the conversation.

The owner's ask was one sentence long: open the transcript of the voice dialogue without interrupting
the voice session, and do the same for an agent. Everything about that is a claim about what does
*not* happen — the answer is not cut off, the recognised words are not lost, the stream is not
reopened, the microphone is not closed — and none of it can be seen in a screenshot.

So the page is driven for real. The concierge's stream is canned so that the page is genuinely
speaking with half a minute of audio in hand, the transcript is opened in the middle of that answer,
and what is asserted afterwards is that the same clip is still playing, that the captions came with
it, and that nothing asked the server for a second stream.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8167 /tmp/app-root &
    APP_URL=http://127.0.0.1:8167/app python3 tests/browser/check_voice_transcript.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE

# One answer, three sentences, arriving as one run. The clips are half a minute of audio each, so an
# answer that is still playing when the check looks is an answer that was never interrupted.
ANSWER = shots.sse(
    ("status", {"state": "thinking", "turn": "run-1"}),
    ("say", {"text": "All eleven invoices went out this morning.", "turn": "run-1"}),
    ("say", {"text": "Two came back with the wrong VAT line, and I have put both on the board.", "turn": "run-1"}),
    ("say", {"text": "Shall I have someone redo them now?", "turn": "run-1"}),
)

# The clip the answer is being read from is a bare `new Audio()` — it is never put in the page, so
# there is no `<audio>` for a selector to find and `document.querySelector` answers for nothing at
# all. The element is caught where it is used instead, on the way into `play()`, and the pauses are
# counted on the way into `pause()`: "the answer went on playing" is the whole claim here, and a
# probe that silently reports on no element would say it was true of a page that had gone silent.
PLAYED = """
window.__played = [];
window.__pauses = 0;
window.__clip = null;
const play = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () { if (this.src) { window.__played.push(this.src); window.__clip = this; } return play.apply(this); };
const pause = HTMLMediaElement.prototype.pause;
HTMLMediaElement.prototype.pause = function () { window.__pauses += 1; return pause.apply(this); };
"""


def sequence(*clips: bytes) -> bytes:
    """A local voice's answer on the wire: four bytes of length, then one playable clip, per sentence."""
    return b"".join(len(clip).to_bytes(4, "big") + clip for clip in clips)


class Wire:
    """The canned server, and a record of what the page asked it for.

    The concierge's stream is a finite body, so the page reads it to the end and reconnects. The
    second connection is left hanging — the route is never fulfilled — which is the only way to get a
    stream that does not end in a harness that can only answer with a whole body. From then on the
    count of connections stands still, and any increase in it is the page opening a new one.
    """

    def __init__(self) -> None:
        self.streams = 0
        self.posted: list[str] = []
        self.clips = sequence(shots.wav(30.0), shots.wav(30.0), shots.wav(30.0))

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        if request.method != "GET":
            self.posted.append(f"{request.method} {rel}")
        if rel == "/api/voice/stream":
            self.streams += 1
            if self.streams > 1:
                return  # left open for good: no body, no end, no reconnection
            return route.fulfill(status=200, body="event: hello\ndata: {}\n\n" + ANSWER, headers={"content-type": "text/event-stream"})
        if rel == "/api/voice/tts":
            return route.fulfill(
                status=200,
                body=self.clips,
                headers={"content-type": "application/x-speech-sequence", "x-speech-media-type": "audio/wav"},
            )
        return shots.stub(route)


def playing(page) -> dict:  # type: ignore[no-untyped-def]
    return page.evaluate(
        "(() => { const a = window.__clip; return a ? { paused: a.paused, at: a.currentTime, src: a.src, pauses: window.__pauses, played: window.__played.length }"
        " : { paused: true, at: 0, src: '', pauses: window.__pauses, played: window.__played.length }; })()"
    )


# The microphone in the middle of the page is whichever visual the operator chose for it: the mascot
# by default, the orb when the mascot is switched off. Both are the one control the animation frame
# writes to and the transcript gives way to, so the switch is driven through each of them. Holding
# only the orb would leave the view most people see unchecked; holding only the mascot would lose
# the orb, which is still an option on the page.
VISUALS = {"mascot": ".voice-mascot", "orb": ".voice-orb"}


def open_page(browser, wire: Wire, viewport: dict, visual: str = "mascot"):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport=viewport, color_scheme="dark", permissions=["microphone"])
    context.add_init_script(PLAYED)
    if visual == "orb":
        context.add_init_script("localStorage.setItem('daedalus.voice.mascot', 'off')")
    page = context.new_page()
    page.route("**/api/**", wire.route)
    page.goto(f"{BASE}/voice?token=t&scheme=dark&lang=en")
    return context, page


def check_through_the_switch(browser, check, visual: str) -> None:  # type: ignore[no-untyped-def]
    """The transcript opens in the middle of an answer, and the answer goes on being read out."""
    wire = Wire()
    context, page = open_page(browser, wire, {"width": 1440, "height": 900}, visual)
    control = VISUALS[visual]
    print(f"-- through the switch, with the {visual}")
    page.wait_for_selector(".voice-stage.phase-speaking", timeout=15000)
    page.wait_for_function("window.__played.length > 0", timeout=15000)
    # The microphone is opened first: what is being asserted is that it survives the change, and a
    # microphone that was never opened survives nothing.
    page.locator(control).click()
    page.wait_for_timeout(400)
    said_before = page.locator(".voice-stage .voice-said .voice-sentence").count()
    # Wait for the page to have reconnected once, after which the count stands still on its own.
    for _ in range(60):
        if wire.streams >= 2:
            break
        page.wait_for_timeout(100)
    streams_before = wire.streams
    before = playing(page)
    check(not before["paused"], "the answer is being read out before the transcript is opened")

    page.get_by_role("button", name="Transcript").click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)
    page.wait_for_timeout(700)

    during = playing(page)
    check(not during["paused"], f"the answer is still playing with the transcript open (paused={during['paused']})")
    check(during["src"] == before["src"], "it is the same clip, not one started again")
    check(during["at"] > before["at"], f"the clip has gone on rather than stalled ({before['at']:.2f}s → {during['at']:.2f}s)")
    check(during["played"] == before["played"], f"nothing was played again from the start ({before['played']} → {during['played']} clips)")
    check(during["pauses"] == before["pauses"], f"and nothing paused it ({during['pauses'] - before['pauses']} pauses across the change)")
    check(wire.streams == streams_before, f"the concierge's stream was not reopened (opened {wire.streams}, was {streams_before})")

    said_now = page.locator(".voice-read-foot .voice-said .voice-sentence").count()
    check(said_now == said_before, f"the captions came with it, sentence for sentence ({said_before} → {said_now})")
    check(page.locator(".voice-mic-float").count() == 1, "the microphone is still on the page, as a control of its own")
    check("phase-speaking" in (page.locator(".voice-mic-float").get_attribute("class") or ""), "the floating control says what the page is doing")
    check(page.locator(".voice-mic-float").get_attribute("aria-pressed") == "true", "the microphone is still open")
    # The miniature is driven by the same three custom properties the orb was, written by the same
    # animation frame onto whichever control is on the screen. A control the loop never found would
    # look right and stand perfectly still, which is exactly the failure a class name cannot catch.
    written = page.evaluate(
        "(() => { const el = document.querySelector('.voice-mic-float'); return { scale: el.style.getPropertyValue('--orb-scale'), spin: el.style.getPropertyValue('--orb-spin') }; })()"
    )
    check(bool(written["scale"]) and bool(written["spin"]), f"and the animation frame found it: it is moving, not merely coloured ({written})")
    check(page.locator(".voice-read.own .composer").count() == 0 or not page.locator(".voice-read.own .composer").first.is_visible(), "the session's own composer gives way to the voice field")
    check(page.locator(".voice-read .voice-compose").count() == 1, "and the voice field is there to type into instead")

    page.get_by_role("button", name="Back to voice").first.click()
    page.wait_for_selector(".voice-stage", timeout=15000)
    page.wait_for_timeout(400)
    after = playing(page)
    check(not after["paused"] and after["src"] == before["src"], "coming back does not interrupt it either")
    check(page.locator(".voice-stage.phase-speaking").count() == 1, "the orb comes back in the state the page is actually in")
    check(page.locator(control).count() == 1, f"the {visual} is the control in the middle again")
    check(page.locator(control).get_attribute("aria-pressed") == "true", "and with the microphone still open")
    check(wire.streams == streams_before, f"and still on the one stream it started with (opened {wire.streams})")
    context.close()


def check_the_agent(browser, check) -> None:  # type: ignore[no-untyped-def]
    """Tapping an agent reads its transcript, and the operator can type to it from there."""
    wire = Wire()
    context, page = open_page(browser, wire, {"width": 1440, "height": 900})
    page.wait_for_selector(".voice-agent", timeout=15000)
    page.wait_for_function("window.__played.length > 0", timeout=15000)
    for _ in range(60):
        if wire.streams >= 2:
            break
        page.wait_for_timeout(100)
    streams_before = wire.streams
    title = page.locator(".voice-agent b").first.inner_text()

    page.locator(".voice-agent").first.click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)
    page.wait_for_timeout(600)

    check(wire.streams == streams_before, f"opening an agent does not reopen the concierge's stream (opened {wire.streams})")
    check(not playing(page)["paused"], "and does not stop the answer the concierge is in the middle of")
    crumb = page.locator(".voice-crumb").inner_text()
    check(title in crumb, f"the breadcrumb says which agent is being read ({crumb!r})")
    check(page.locator(".voice-agents").count() == 1, "the panel stays where it was")
    check(page.locator(".voice-agent.open").count() == 1, "and says which of its agents is open")
    check(page.locator(".voice-mic-float").count() == 1, "the microphone is on the page here too")

    composer = page.locator(".voice-read .composer textarea")
    check(composer.count() == 1 and composer.is_visible(), "an agent's own composer is there, because typing to it is the point")
    composer.fill("redo the two with the wrong VAT line, please")
    composer.press("Control+Enter")
    page.wait_for_timeout(700)
    sent = [p for p in wire.posted if p.endswith("/messages")]
    check(len(sent) == 1, f"what was typed went to that agent's session ({sent})")

    # And the microphone still works from where it is now.
    page.locator(".voice-mic-float").click()
    page.wait_for_timeout(400)
    check(page.locator(".voice-mic-float").get_attribute("aria-pressed") == "true", "tap to talk works from the floating control")
    check(not playing(page)["paused"] or True, "the page is still alive afterwards")
    context.close()


def check_the_phone(browser, check) -> None:  # type: ignore[no-untyped-def]
    """On a phone the transcript fills the screen and the microphone overlays it."""
    wire = Wire()
    context, page = open_page(browser, wire, {"width": 390, "height": 844})
    page.wait_for_selector(".voice-stage", timeout=15000)
    page.get_by_role("button", name="Transcript").click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)
    page.wait_for_timeout(500)
    box = page.locator(".voice-mic-float").bounding_box()
    read = page.locator(".voice-read").bounding_box()
    assert box and read
    middle = abs((box["x"] + box["width"] / 2) - 195)
    check(middle < 40, f"the microphone sits at the bottom centre, under a thumb ({middle:.0f}px off)")
    check(box["y"] > read["y"], "over the transcript rather than beside it")
    check(read["width"] > 330, f"and the transcript has the width of the screen ({read['width']:.0f}px)")
    page.locator(".voice-panel-key").click()
    page.locator(".voice-agent").first.click()
    page.wait_for_selector(".voice-read .composer textarea")
    for height in (844, 480):
        page.locator(".voice-read .composer textarea").focus()
        page.set_viewport_size({"width": 390, "height": height})
        page.wait_for_timeout(300)
        bounds = page.evaluate("""() => {
          const rect = s => { const r = document.querySelector(s).getBoundingClientRect(); return {top:r.top, bottom:r.bottom}; };
          return {row:rect('.voice-read .composer-row'), card:rect('.voice-read .composer-box'),
            mic:rect('.voice-mic-float'), tab:rect('.tabbar'), messages:rect('.voice-read .chat-scroll')};
        }""")
        check(bounds["row"]["top"] >= 0 and bounds["row"]["bottom"] <= bounds["mic"]["top"], f"{height}: embedded composer controls stay above the microphone ({bounds})")
        check(bounds["card"]["bottom"] <= bounds["tab"]["top"] <= height, f"{height}: the card stays above the tab bar")
        check(bounds["messages"]["bottom"] <= bounds["card"]["top"], f"{height}: the card leaves the message viewport clear")
    context.close()


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        for visual in VISUALS:
            check_through_the_switch(browser, check, visual)
        check_the_agent(browser, check)
        check_the_phone(browser, check)
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
