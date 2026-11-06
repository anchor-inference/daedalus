"""Settings → Public address and a service's QR for a phone, driven in a real browser.

The unit tests pin the words and what is written. What only a browser shows is the whole page: on a
server the section is neither listed nor drawn, and a typed address falls back to the index; natively
it holds each provider's setup, a token that is written and never comes back, the state in plain words
for every moment of a tunnel's life and every failure, the address with its QR, what is exposed, and
one line and no controls where it cannot work. It reads the host while it is open and stops when it is
left. A service's Access sheet opens the QR for each way, says what a code does, forgets phones, and
offers nothing to a service with no port. The agent link's form says that links stay on this machine.
All in English and Russian, at 1280 and at 390 px, where nothing may run off the side.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_public_address.py

``SHOTS=<dir>`` writes ``address-<state>-<en|ru>-<1280|390>.png`` and ``phone-<en|ru>-<1280|390>.png``.
Exit status is the number of failed checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeGuests, PublicAddressStub, expect_app, qr_rows  # noqa: E402
from check_overflow_menu import SERVICES, service  # noqa: E402
from check_overflow_menu import stub as service_stub  # noqa: E402
from screenshots import S1, UNHANDLED, detail, respond  # noqa: E402
from screenshots import stub as chat_stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
NO_PORT = {**service(2, shared=False), "port": None, "url": None}

WORDS = {
    "en": {
        "title": "Public address", "off": "Off", "answering": "Answering", "checking": "Connected, checking from outside", "reconnecting": "Reconnecting…",
        "installing": "Downloading the tunnel program…", "starting": "Starting…", "sign_in": "Waiting for you to sign in", "signin": "Sign in",
        "notapp": "The app itself is not exposed.", "down": "When the tunnel is down or the app is closed, the address does not answer.",
        "saved": "Saved", "replace": "Replace", "forget": "Forget", "save": "Save", "signout": "Sign out", "old": "The desktop app needs updating before it can open a public address.",
        "tunneld": "This desktop app has no tunnel program: update the app.", "launcher": "The public address needs the desktop app running beside this installation.",
        "access": "Access", "wifi": "Same Wi-Fi", "tunnel": "Through the public address", "open": "Open on a phone", "terms": "The code works once, for 10 minutes; that phone then keeps access for 12 hours.",
        "forgetphones": "Forget phones", "none": "Neither way is available here.", "nonenative": "Neither way is available. Open a public address:",
        "guest": "Until a public address is on, links will only work on this machine.", "actions": "Session actions", "menu": "Agent link…", "make": "Make a link",
        "codes": {"no_client": "The tunnel program is missing", "token_rejected": "The provider refused this token", "blocked": "This computer would not run", "https_off": "Turn on HTTPS certificates",
                  "funnel_off": "Allow Funnel", "server_refused": "Your server refused", "not_configured": "Setup is not finished", "no_tunneld": "This desktop app has no tunnel program",
                  "download_failed": "The tunnel program did not download", "other": "The tunnel stopped"},
    },
    "ru": {
        "title": "Публичный адрес", "off": "Выключен", "answering": "Отвечает", "checking": "Подключён, проверяем снаружи", "reconnecting": "Переподключается…",
        "installing": "Скачиваем программу туннеля…", "starting": "Запускается…", "sign_in": "Ждём, пока вы войдёте", "signin": "Войти",
        "notapp": "Само приложение остаётся закрытым.", "down": "Когда туннель упал или приложение закрыто, адрес не отвечает.",
        "saved": "Сохранён", "replace": "Заменить", "forget": "Забыть", "save": "Сохранить", "signout": "Выйти", "old": "Чтобы открыть публичный адрес, обновите настольное приложение.",
        "tunneld": "В этом настольном приложении нет программы туннеля: обновите приложение.", "launcher": "Для публичного адреса рядом с этой установкой должно работать настольное приложение.",
        "access": "Доступ", "wifi": "В той же сети Wi-Fi", "tunnel": "Через публичный адрес", "open": "Открыть на телефоне", "terms": "Код сработает один раз в течение 10 мин, а телефон сохранит доступ на 12 ч.",
        "forgetphones": "Забыть телефоны", "none": "Здесь ни один путь недоступен.", "nonenative": "Ни один путь недоступен. Откройте публичный адрес:",
        "guest": "Пока публичный адрес выключен, ссылки работают только на этом компьютере.", "actions": "Действия с сессией", "menu": "Ссылка для агента…", "make": "Сделать ссылку",
        "codes": {"no_client": "Нет программы туннеля", "token_rejected": "Провайдер не принял токен", "blocked": "Компьютер не дал запустить", "https_off": "Включите HTTPS-сертификаты",
                  "funnel_off": "Разрешите Funnel", "server_refused": "Ваш сервер отказал", "not_configured": "Настройка не закончена", "no_tunneld": "В этом настольном приложении нет программы",
                  "download_failed": "Программа туннеля не скачалась", "other": "Туннель остановился"},
    },
}
UP = {"enabled": True, "state": "up", "address": PublicAddressStub.ADDRESS, "confirmed": True, "qr": qr_rows(PublicAddressStub.ADDRESS)}


class Host:
    """What the page talks to: the tunnel stub, or nothing of it on a server."""

    def __init__(self, tunnel: PublicAddressStub | None = None, *, windows: bool = False) -> None:
        self.tunnel = tunnel
        self.windows = windows


HOST = Host()


def stub(route) -> None:  # type: ignore[no-untyped-def]
    path = route.request.url.split("?", 1)[0]
    if path.endswith("/api/capabilities") and HOST.windows:
        return route.fulfill(status=200, content_type="application/json", body=json.dumps({**api_stub.GATES["/api/capabilities"], "reveal": {"available": True, "platform": "windows"}}))
    if path.endswith("/api/services"):
        return route.fulfill(status=200, content_type="application/json", body=json.dumps([*SERVICES, NO_PORT]))
    if HOST.tunnel is not None and HOST.tunnel.fulfil(route):
        return None
    return service_stub(route)


def overflow(page: Page) -> list[str]:
    """Everything that runs off the side of the window, and the page's own sideways scroll."""
    return page.evaluate(
        """() => {
            const out = [];
            const width = document.documentElement.clientWidth;
            if (document.documentElement.scrollWidth > width) out.push(`page ${document.documentElement.scrollWidth} > ${width}`);
            for (const element of document.querySelectorAll('.main *, .sheet *')) {
                const box = element.getBoundingClientRect();
                if (box.width && box.right > width + 1 && !element.closest('.hscroll, svg')) out.push(`${element.tagName}.${element.className} ${Math.round(box.right)}`);
            }
            return out.slice(0, 6);
        }"""
    )


def open_section(browser, lang: str, width: int, tunnel: PublicAddressStub | None, *, windows: bool = False, scheme: str = "dark"):  # type: ignore[no-untyped-def]
    global HOST
    HOST = Host(tunnel, windows=windows)
    phone = width < 600
    context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, color_scheme=scheme, is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1, permissions=["clipboard-read", "clipboard-write"])
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/settings/address?token=t&scheme={scheme}&lang={lang}")
    return context, page, errors


def status(page: Page) -> str:
    return page.locator("[data-address-status]").inner_text().strip()


def picture(page: Page, name: str, lang: str, width: int) -> None:
    if SHOTS:
        # The column scrolls inside the window, so a full-page picture would stop at the fold: the window
        # is made tall enough for all of it, for the picture.
        size = page.viewport_size
        page.set_viewport_size({"width": size["width"], "height": 1500})
        page.wait_for_timeout(250)
        page.screenshot(path=f"{SHOTS}/address-{name}-{lang}-{width}.png")
        page.set_viewport_size(size)


def run_states(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Every moment of a tunnel's life, in words. The stub changes under the open page, as a tunnel does."""
    words = WORDS[lang]
    where = f"{lang} {width}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    tunnel = PublicAddressStub()
    context, page, errors = open_section(browser, lang, width, tunnel)
    try:
        page.wait_for_selector("[data-address]", timeout=15000)
    except Exception:  # noqa: BLE001 - reported
        say("the section did not draw natively")
        context.close()
        return
    if status(page) != words["off"]:
        say(f"off reads {status(page)!r}")
    if page.locator("[data-address-shown]").count():
        say("an address is drawn while off")
    if page.locator("[data-address-exposed] [data-exposed=links]").inner_text() != "2" or page.locator("[data-exposed=services]").inner_text() != "1":
        say("the exposed counts are wrong")
    text = page.locator("[data-address-exposed]").inner_text()
    if words["notapp"] not in text or words["down"] not in text:
        say("the lines about the app and about a tunnel that is down are missing")
    picture(page, "off", lang, width)

    steps = [
        ("installing", {"enabled": True, "state": "installing"}, words["installing"]),
        ("starting", {"enabled": True, "state": "starting"}, words["starting"]),
        ("up-checking", {**UP, "confirmed": False}, words["checking"]),
        ("up", UP, words["answering"]),
        ("reconnecting", {**UP, "state": "reconnecting", "confirmed": False}, words["reconnecting"]),
        ("sign_in", {"enabled": True, "provider": "tailscale", "state": "sign_in", "address": "", "qr": [], "confirmed": False, "sign_in": "https://login.tailscale.com/a/abc"}, words["sign_in"]),
    ]
    for name, change, word in steps:
        tunnel.view.update(change)
        try:
            expect(page.locator("[data-address-status]")).to_have_text(word, timeout=6000)
        except AssertionError:
            say(f"{name} reads {status(page)!r}, not {word!r}")
        off = overflow(page)
        if off:
            say(f"{name} runs off the side: {off}")
        picture(page, name, lang, width)
        if name == "up":
            shown = page.locator("[data-address-shown]")
            if shown.locator("input").input_value() != tunnel.ADDRESS:
                say("the address is not shown")
            if shown.locator("svg.totp-qr").count() != 1:
                say("the address has no QR")
            shown.locator("button").click()
            page.wait_for_timeout(100)
            if context.pages and width >= 600 and page.evaluate("navigator.clipboard.readText()") != tunnel.ADDRESS:
                say("Copy did not put the address on the clipboard")
        if name == "up-checking" and page.locator("[data-address-shown]").count() != 1:
            say("a connected address is not shown")
        if name == "sign_in":
            link = page.get_by_role("link", name=words["signin"], exact=True)
            if link.get_attribute("href") != "https://login.tailscale.com/a/abc" or link.get_attribute("target") != "_blank" or link.get_attribute("rel") != "noopener":
                say("the sign-in is not a link that opens a new window with noopener")

    # Every failure once per language: the words do not change with the width, and each waits for a poll.
    for code, word in list(words["codes"].items())[:None if width >= 600 else 1]:
        tunnel.view.update(enabled=True, provider="cloudflare", state="failed", code=code, detail="ngrok said ERR_NGROK_105", address="", qr=[], sign_in="", confirmed=False)
        try:
            expect(page.locator("[data-address-status]")).to_contain_text(word, timeout=6000)
        except AssertionError:
            say(f"failure {code} reads {status(page)!r}")
        if "ERR_NGROK" in status(page):
            say(f"failure {code} shows the English detail as its main text")
    tunnel.view.update(code="funnel_off")
    page.wait_for_timeout(3300)
    off = overflow(page)
    if off:
        say(f"a failure runs off the side: {off}")
    picture(page, "failed", lang, width)
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()


def run_setup(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Each provider's shortest setup, and that a token goes one way."""
    words = WORDS[lang]
    where = f"{lang} {width}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    tunnel = PublicAddressStub()
    tunnel.provider("ngrok")["client"] = False
    context, page, errors = open_section(browser, lang, width, tunnel, windows=True)
    page.wait_for_selector("[data-address]", timeout=15000)

    if page.locator("[data-address-provider]").count() != 4:
        say("the four providers are not offered")
    if page.locator("[data-token], .settings-block").count():
        say("a quick tunnel asks for something")

    page.locator("[data-address-provider=ngrok]").click()
    page.wait_for_selector("#address-ngrok-token")
    if tunnel.puts[-1] != {"provider": "ngrok"}:
        say(f"choosing ngrok sent {tunnel.puts[-1]}")
    link = page.locator("[data-address-get=ngrok] a")
    if link.get_attribute("href") != "https://ngrok.com/download" or link.get_attribute("rel") != "noopener":
        say("no link to download ngrok")
    token = "2abcSECRETdef"
    field = page.locator("#address-ngrok-token")
    if field.get_attribute("type") != "password":
        say("the token field shows what is typed")
    field.fill(token)
    picture(page, "ngrok", lang, width)
    page.get_by_role("button", name=words["save"], exact=True).click()
    page.wait_for_selector("[data-token=saved]")
    if tunnel.puts[-1] != {"ngrok_token": token}:
        say(f"saving the token sent {tunnel.puts[-1]}")
    if words["saved"] not in page.locator("[data-token=saved]").inner_text():
        say("a kept token does not say it is saved")
    if token in page.content():
        say("the token is in the page after saving it")
    page.get_by_role("button", name=words["replace"], exact=True).click()
    if page.locator("#address-ngrok-token").input_value() != "":
        say("Replace fills the field")
    page.get_by_role("button", name=words["save"], exact=True).wait_for()
    page.locator("#address-ngrok-token").fill("  ")
    if page.get_by_role("button", name=words["save"], exact=True).is_enabled():
        say("a blank token can be saved")
    page.locator("#address-ngrok-token").fill("")
    page.locator("[data-token=empty] button:not([type=submit])").click()
    page.get_by_role("button", name=words["forget"], exact=True).click()
    page.wait_for_selector("[data-token=empty]")
    if tunnel.puts[-1] != {"ngrok_token": ""}:
        say(f"forgetting the token sent {tunnel.puts[-1]}")

    # A refusal arrives in the launcher's words and leaves what was typed in its field.
    tunnel.refuse = "that does not look like a token"
    page.locator("#address-ngrok-token").fill("not a token")
    page.get_by_role("button", name=words["save"], exact=True).click()
    page.wait_for_selector(".address-problem")
    if page.locator("#address-ngrok-token").input_value() != "not a token":
        say("a refused token is gone from its field")

    page.locator("[data-address-provider=tailscale]").click()
    page.wait_for_selector("[data-address-provider=tailscale].on")
    if page.locator("[data-token], .settings-block").count():
        say("Tailscale asks for something before its sign-in")
    tunnel.view.update(state="off")
    tunnel.provider("tailscale")["signed_in"] = True
    button = page.get_by_role("button", name=words["signout"], exact=True)
    button.wait_for(timeout=6000)
    picture(page, "tailscale", lang, width)
    button.click()
    expect(button).to_have_count(0)
    if tunnel.puts[-1].get("sign_out") is not True:
        say(f"signing out sent {tunnel.puts[-1]}")

    page.locator("[data-address-provider=frp]").click()
    page.wait_for_selector("#address-frp-server")
    page.locator("#address-frp-server").fill("vps.example.test:7000")
    page.locator("#address-frp-domain").click()
    page.wait_for_function("() => document.querySelector('[data-address]') !== null")
    page.locator("#address-frp-domain").fill("tunnel.example.test")
    page.locator("#address-frp-token").click()
    page.wait_for_timeout(300)
    sent = {key: value for put in tunnel.puts for key, value in put.items() if key.startswith("frp_")}
    if sent != {"frp_server": "vps.example.test:7000", "frp_domain": "tunnel.example.test"}:
        say(f"frp's fields sent {sent}")
    if "desktop/README.md#public-address" not in (page.locator("[data-provider=frp] .sub a").get_attribute("href") or ""):
        say("frp has no link to its documentation")
    if not page.locator("[data-address-defender]").count():
        say("frp on Windows does not mention Defender")
    off = overflow(page)
    if off:
        say(f"frp's setup runs off the side: {off}")
    picture(page, "frp", lang, width)
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()

    # The Defender note belongs to Windows alone.
    context, page, errors = open_section(browser, lang, width, PublicAddressStub(provider="frp"))
    page.wait_for_selector("[data-provider=frp]", timeout=15000)
    page.wait_for_timeout(300)
    if page.locator("[data-address-defender]").count():
        say("frp away from Windows mentions Defender")
    context.close()


def run_unavailable(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    cases = [("old_launcher", {"native": True, "available": False, "reason": "old_launcher"}, words["old"]), ("no_launcher", {"native": True, "available": False, "reason": "no_launcher"}, words["launcher"]),
             ("no_tunneld", None, words["tunneld"])]
    for reason, view, word in cases:
        tunnel = PublicAddressStub(tunneld=False)
        if view is not None:
            tunnel.view = view
        context, page, _errors = open_section(browser, lang, width, tunnel)
        box = page.locator(f"[data-address-unavailable={reason}]")
        try:
            box.wait_for(timeout=15000)
            if box.inner_text().strip() != word:
                problems.append(f"{lang} {width}px {reason}: reads {box.inner_text()!r}")
            if page.locator(".settings-col .switch, [data-address-provider]").count():
                problems.append(f"{lang} {width}px {reason}: controls are drawn")
        except Exception:  # noqa: BLE001 - reported
            problems.append(f"{lang} {width}px {reason}: nothing said")
        picture(page, reason, lang, width)
        context.close()


def run_polling(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    tunnel = PublicAddressStub()
    context, page, _errors = open_section(browser, "en", 1280, tunnel)
    page.wait_for_selector("[data-address]", timeout=15000)
    start = tunnel.reads
    page.wait_for_timeout(6500)
    if tunnel.reads - start < 2:
        problems.append(f"the open section read the host {tunnel.reads - start} times in 6.5 s")
    page.locator(".settings-link", has_text="Appearance").click()
    page.wait_for_timeout(300)
    left = tunnel.reads
    page.wait_for_timeout(6500)
    if tunnel.reads != left:
        problems.append(f"the section read the host {tunnel.reads - left} times after it was left")
    context.close()


def run_server(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"server {lang} {width}px"
    context, page, errors = open_section(browser, lang, width, None)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    if page.locator("[data-address], [data-address-unavailable]").count():
        problems.append(f"{where}: the section is drawn")
    if page.locator(".settings-link", has_text=words["title"]).count():
        problems.append(f"{where}: the index lists the section")
    if errors:
        problems.append(f"{where}: page errors: {errors[:3]}")
    context.close()


def open_sheet(page: Page, words: dict, index: int = 1) -> None:
    page.wait_for_selector(".service-row", timeout=15000)
    page.locator(".service-row").nth(index).locator("button[aria-haspopup='menu']").click()
    page.get_by_role("menuitem", name=words["access"], exact=True).or_(page.locator(".menu[role='menu'] button", has_text=words["access"])).first.click()
    page.wait_for_selector("[data-service-phone]", timeout=5000)


def run_phone(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"phone sheet {lang} {width}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    tunnel = PublicAddressStub()
    context, page, errors = open_section(browser, lang, width, tunnel)
    page.goto(f"{BASE}/services?token=t&lang={lang}")
    try:
        open_sheet(page, words)
    except Exception as error:  # noqa: BLE001 - reported
        say(f"the sheet has no phone control: {error}")
        context.close()
        return
    if page.locator("[data-way]").count():
        say("a code was shown before it was asked for")
    if tunnel.phones:
        say("opening the sheet minted a code")
    page.get_by_role("button", name=words["open"]).click()
    page.wait_for_selector("[data-way]")
    if tunnel.phones != ["svc-1"]:
        say(f"the press asked for {tunnel.phones}")
    ways = page.locator("[data-way]")
    if ways.count() != 2 or ways.nth(0).get_attribute("data-way") != "wifi":
        say("Wi-Fi is not the first of two ways")
    if words["wifi"] not in ways.nth(0).inner_text() or words["tunnel"] not in ways.nth(1).inner_text():
        say("the ways are not labelled")
    if page.locator("[data-way] svg.totp-qr").count() != 2:
        say("the ways have no QR")
    if page.locator("[data-phone-terms]").inner_text().strip() != words["terms"]:
        say(f"the terms read {page.locator('[data-phone-terms]').inner_text()!r}")
    off = overflow(page)
    if off:
        say(f"the sheet runs off the side: {off}")
    if SHOTS:
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/phone-{lang}-{width}.png")
    page.locator("[data-phone-forget]").click()
    expect(page.locator("[data-way]")).to_have_count(0)
    if tunnel.forgotten != ["svc-1"]:
        say(f"forgetting reached {tunnel.forgotten}")
    tunnel.ways = []
    page.locator("[data-phone-open]").click()
    page.wait_for_selector("[data-phone-none]")
    text = page.locator("[data-phone-none]").inner_text()
    if words["nonenative"] not in text or words["title"] not in text:
        say(f"no way reads {text!r}")
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-service-phone]", state="detached", timeout=3000)

    # A service with no port has no Access sheet, so no way to a phone either.
    page.locator(".service-row").nth(2).locator("button[aria-haspopup='menu']").click()
    page.wait_for_selector(".menu[role='menu']")
    if page.locator(".menu[role='menu'] button", has_text=words["access"]).count():
        say("a service with no port offers Access")
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()

    # On a server a service's QR still works, and an empty answer names no Settings section.
    context, page, errors = open_section(browser, lang, width, None)
    page.goto(f"{BASE}/services?token=t&lang={lang}")
    open_sheet(page, words)
    page.locator("[data-phone-open]").click()
    page.wait_for_selector("[data-phone-none]")
    if page.locator("[data-phone-none]").inner_text().strip() != words["none"]:
        say(f"on a server no way reads {page.locator('[data-phone-none]').inner_text()!r}")
    context.close()


def guest_route(tunnel: PublicAddressStub | None):  # type: ignore[no-untyped-def]
    """The chat's stub with the tunnel in front of it."""

    def route(handled) -> None:  # type: ignore[no-untyped-def]
        path = handled.request.url.split("?", 1)[0]
        if tunnel is not None and tunnel.fulfil(handled):
            return None
        if handled.request.method == "GET" and path.endswith(f"/api/sessions/{S1}"):
            return respond(handled, detail(S1))
        return chat_stub(handled)

    return route


def run_guest(browser, lang: str, width: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """The agent link's form natively with no address up, with one up, and on a server."""
    words = WORDS[lang]
    where = f"guest form {lang} {width}px"
    for name, tunnel in (("down", PublicAddressStub()), ("up", PublicAddressStub(**UP)), ("server", None)):
        api_stub.GUESTS = FakeGuests()

        phone = width < 600
        context = browser.new_context(viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, has_touch=phone)
        page = context.new_page()
        page.route("**/api/**", guest_route(tunnel))
        page.goto(f"{BASE}/agents/{S1}?token=t&lang={lang}")
        try:
            page.get_by_role("button", name=words["actions"], exact=True).click(timeout=15000)
            page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
            sheet = page.locator(".ph-session-menu")
            (sheet.get_by_role("button", name=words["menu"], exact=True) if sheet.count() else page.get_by_role("menuitem", name=words["menu"], exact=True)).click()
            page.get_by_role("button", name=words["make"], exact=True).click()
            page.wait_for_selector("[data-guest-form]", timeout=5000)
            page.wait_for_timeout(300)
            note = page.locator("[data-address-note]")
            if name == "down":
                if words["guest"] not in note.inner_text() or words["title"] not in note.locator("a").inner_text():
                    problems.append(f"{where}: {note.inner_text()!r}")
                if SHOTS:
                    page.screenshot(path=f"{SHOTS}/address-guest-{lang}-{width}.png")
            elif note.count():
                problems.append(f"{where}: {name} draws the note")
        except Exception as error:  # noqa: BLE001 - reported
            problems.append(f"{where}: {name}: {str(error).splitlines()[0]}")
        context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM if Path(CHROMIUM).exists() else None)
        for lang in ("en", "ru"):
            for width in (1280, 390):
                run_server(browser, lang, width, problems)
                run_states(browser, lang, width, problems)
                run_setup(browser, lang, width, problems)
                run_unavailable(browser, lang, width, problems)
                run_phone(browser, lang, width, problems)
                run_guest(browser, lang, width, problems)
        run_polling(browser, problems)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("public address: all checks passed")
    return len(problems) + UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
