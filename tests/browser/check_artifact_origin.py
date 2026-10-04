"""Open real artifact routes in Chromium without granting their scripts the app origin."""

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from playwright.sync_api import sync_playwright

# A shared browser virtualenv may have an editable install from another worktree. Import the
# implementation beside this check so a successful security probe covers the bytes under review.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from daedalus.extensions.api_files import register  # noqa: E402

scratch = tempfile.TemporaryDirectory(prefix="artifact-origin-")
root = Path(scratch.name)
script = "try{document.cookie;document.documentElement.dataset.cookie='readable'}catch{document.documentElement.dataset.cookie='blocked'};try{localStorage.getItem('fixture');document.documentElement.dataset.storage='readable'}catch{document.documentElement.dataset.storage='blocked'};document.documentElement.dataset.ran='yes';fetch('/api/private',{credentials:'include'}).then(r=>r.text()).then(t=>document.documentElement.dataset.read=t).catch(()=>document.documentElement.dataset.read='blocked');"
(root / "sample.html").write_text("<!doctype html><title>Probe</title><script>" + script + "</script><p>Local artifact</p>")
(root / "sample.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script><![CDATA[' + script + ']]></script><text x="10" y="30">Local artifact</text></svg>')
(root / "sample.xml").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script><![CDATA[' + script + ']]></script><text x="10" y="30">Local artifact</text></svg>')
(root / "sample.pdf").write_bytes((Path(__file__).parents[2] / "skills/theme-factory/theme-showcase.pdf").read_bytes())
items = {"html": SimpleNamespace(id="html", name="sample.html", mime="text/html"), "svg": SimpleNamespace(id="svg", name="sample.svg", mime="image/svg+xml")}
items["xml"] = SimpleNamespace(id="xml", name="sample.xml", mime="text/xml")
items["pdf"] = SimpleNamespace(id="pdf", name="sample.pdf", mime="application/pdf")
class Files:
    async def get(self, identity):
        return items.get(identity)
    def path_of(self, item):
        return root / item.name
api = FastAPI()
hits = []
async def auth(request: Request):
    if request.cookies.get("probe_auth") != "operator-fixture":
        raise HTTPException(401)
    return {"user_id": 1, "via": "cookie"}
register(api, SimpleNamespace(manager=SimpleNamespace(files=Files())), auth)
@api.get("/api/private")
async def private(request: Request):
    await auth(request)
    hits.append(request.url.path)
    return "controlled-private-sentinel"
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(api, log_level="error", lifespan="off"))
thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
thread.start()
for _ in range(100):
    if server.started:
        break
    time.sleep(.05)
assert server.started
result = []
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM", "/usr/local/bin/chromium"), headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.add_cookies([{"name": "probe_auth", "value": "operator-fixture", "url": f"http://127.0.0.1:{port}"}])
        page = context.new_page()
        for kind in items:
            hits.clear()
            response = page.goto(f"http://127.0.0.1:{port}/api/files/{kind}/download")
            if kind == "pdf":
                assert response.status == 200 and response.headers.get("x-content-type-options") == "nosniff"
                assert response.headers.get("content-disposition", "").startswith("inline;")
                page.wait_for_timeout(800)
                assert any(child.url.startswith("chrome-extension://") for child in page.frames), "PDF viewer did not load"
                assert not hits, "PDF navigation contacted an authenticated API"
                result.append({"kind": kind, "status": response.status, "private_hits": len(hits)})
                continue
            page.wait_for_function("document.documentElement.dataset.read !== undefined")
            observed = page.evaluate("({...document.documentElement.dataset})")
            assert observed.get("ran") == "yes", "local document scripting remains usable"
            assert observed.get("read") == "blocked" and not hits, "artifact contacted an authenticated API"
            assert observed.get("cookie") == observed.get("storage") == "blocked", "artifact inherited the app origin"
            assert response.headers.get("x-content-type-options") == "nosniff"
            assert response.headers.get("content-disposition", "").startswith("inline;")
            policy = response.headers.get("content-security-policy", "")
            assert "sandbox allow-scripts" in policy and "allow-same-origin" not in policy
            result.append({"kind": kind, "status": response.status, "csp": policy, "ran": observed.get("ran"), "read": observed.get("read"), "private_hits": len(hits)})
        browser.close()
finally:
    server.should_exit = True
    thread.join(5)
    assert not thread.is_alive()
    scratch.cleanup()
print(json.dumps(result))
