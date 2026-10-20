"""End-to-end smoke: the built extension in a real Chromium, talking to a stub native host.

Loads `extension/dist` unpacked into a throwaway profile whose `NativeMessagingHosts` names the stub
under the extension's host name, then checks the round trip a person sees: hello and welcome, ping
and pong, the app's check flashing in the popup, the popup's own check confirmed, a tab created in
the agent's group and driven, and a cookie read refused by the extension itself.

Unbranded Chromium only: branded Chrome ignores `--load-extension` since 137.

    cd extension && npm run build
    CHROMIUM=<chromium binary> python extension/test/e2e/smoke.py

`SMOKE_SCREENSHOT=<file.png>` also saves a picture of the popup after the check.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
EXTENSION = HERE.parent.parent / "dist"
FRAMES = json.loads((HERE.parent / "frames.json").read_text())
EXTENSION_ID = FRAMES["extension_id"]
HOST = FRAMES["native_host"]


def frames(log: Path):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def wait_for(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> int:
    if not (EXTENSION / "manifest.json").exists():
        print("build the extension first: cd extension && npm run build", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        profile = Path(scratch) / "profile"
        log = Path(scratch) / "stub.jsonl"
        hosts = profile / "NativeMessagingHosts"
        hosts.mkdir(parents=True)
        wrapper = Path(scratch) / "host.sh"
        wrapper.write_text(f'#!/bin/sh\nSTUB_LOG="{log}" exec "{sys.executable}" "{HERE / "stub_host.py"}" "$@"\n')
        wrapper.chmod(0o755)
        (hosts / f"{HOST}.json").write_text(json.dumps({
            "name": HOST,
            "description": "relay smoke stub",
            "path": str(wrapper),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
        }))
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile),
                channel="chromium",
                executable_path=os.environ.get("CHROMIUM") or None,
                headless=True,
                args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                assert EXTENSION_ID in worker.url, f"the key did not pin the id: {worker.url}"

                def received(kind):
                    return [f["value"] for f in frames(log) if f["kind"] == "frame" and f["value"].get("t") == kind]

                hello = wait_for(lambda: received("hello"), "hello")[0]
                origin = [f["value"] for f in frames(log) if f["kind"] == "origin"][0]
                assert origin == f"chrome-extension://{EXTENSION_ID}/", origin
                assert hello["proto"] == 1 and hello["profile_name"] == "Chrome", hello
                wait_for(lambda: received("pong"), "pong")

                def results():
                    return {r["id"]: r for r in received("result")}

                wait_for(lambda: 1 in results(), "the notify result")
                assert results()[1]["ok"] and results()[1]["value"] == {"shown": True}

                wait_for(lambda: {2, 3, 4, 5} <= results().keys(), "the tab, evaluate and refused results")
                assert results()[2]["ok"], results()[2]
                assert results()[3]["value"]["result"]["value"] == 42, results()[3]
                assert results()[4]["ok"] is False and results()[4]["error"]["code"] == "refused", results()[4]
                assert results()[5]["ok"], results()[5]

                # The page the browser started with, not a new one: a page Playwright creates opens in
                # the last window, the agent's, beside an agent tab that Chrome then names its opener.
                popup = context.pages[0] if context.pages else context.new_page()
                popup.goto(f"chrome-extension://{EXTENSION_ID}/popup.html")
                # The popup's policy forbids evaluating strings, so it is read through the DOM only.
                wait_for(lambda: popup.inner_text("#install") == "Stub", "the popup to show the installation")
                popup.click("#check")
                popup.wait_for_selector("#check-result[data-ok='true']", timeout=6000)
                text = popup.inner_text("#check-result")
                assert "✓" in text, text
                assert received("check"), "the popup's check never reached the host"
                # The operator's page stayed the operator's.
                assert not received("tabs"), received("tabs")
                if os.environ.get("SMOKE_DEBUG"):
                    print(json.dumps(frames(log), indent=1))
                if os.environ.get("SMOKE_SCREENSHOT"):
                    popup.set_viewport_size({"width": 340, "height": 760})
                    popup.screenshot(path=os.environ["SMOKE_SCREENSHOT"], full_page=True)
                print("smoke: hello/welcome, ping/pong, notify, tab.create, cdp allow and refuse, check/check_ok: ok")
                print("popup:", text)
            finally:
                context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
