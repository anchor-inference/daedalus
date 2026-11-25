"""A chat's Changes tab, as ``daedalus/extensions/api_session_changes.py`` answers it, kept in memory
for the browser harnesses.

Every chat works on the branch ``order-form`` of a shop with two changed files: ``order/form.js``
edited (24 lines in, 4 out) and ``order/sheet.js`` new and untracked (7 lines). The writes answer
the way the host does: a revert wants ``confirm`` and either ``paths`` or ``all``, names the new
files it deleted, and a file it reverted or committed is gone from that chat's list; a commit wants
a message and answers a hash. A harness that needs another tree replaces ``files`` and ``diffs``;
``writes`` is every write as ``(method, path, body)`` for a check to read back.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any
from urllib.parse import parse_qs

# A blank context line of a diff is a single space, written as an escaped space so this file keeps no trailing whitespace.
FORM_DIFF = """diff --git a/order/form.js b/order/form.js
index 3f2a9c1..8d41b07 100644
--- a/order/form.js
+++ b/order/form.js
@@ -11,9 +11,17 @@ import { appendRow, SHEET_ID } from "./sheet.js";
\x20
 export async function submitOrder(form) {
   const data = Object.fromEntries(new FormData(form));
-  await fetch("/api/order", { method: "POST" });
-  form.reset();
+  const row = toSheetRow(data);
+  const res = await appendRow(SHEET_ID, row);
+  if (!res.ok) {
+    showError(form, "Could not send the order");
+    return;
+  }
+  form.reset();
+  showThanks(form, data.name);
 }
\x20
 function toSheetRow(data) {
+  const when = new Date().toISOString();
+  return [when, data.name, data.phone, data.items];
 }
@@ -40,4 +48,16 @@ function showError(form, text) {
 function showThanks(form, name) {
-  form.querySelector(".thanks").hidden = false;
-  form.querySelector(".thanks").textContent = "Thank you!";
+  const box = form.querySelector(".thanks");
+  box.hidden = false;
+  box.textContent = `Thank you, ${name}!`;
+}
+
+export function validPhone(text) {
+  const digits = text.replace(/\\D/g, "");
+  return digits.length >= 10 && digits.length <= 15;
+}
+
+export function clearErrors(form) {
+  form.querySelectorAll(".error").forEach((node) => node.remove());
+  form.classList.remove("has-errors");
+  form.querySelector(".submit").disabled = false;
 }
"""

SHEET_DIFF = """diff --git a/order/sheet.js b/order/sheet.js
new file mode 100644
index 0000000..b5e1f40
--- /dev/null
+++ b/order/sheet.js
@@ -0,0 +1,7 @@
+import { SHEETS_URL } from "./config.js";
+
+export async function appendRow(sheetId, row) {
+  const url = `${SHEETS_URL}/${sheetId}/values:append`;
+  return fetch(url, { method: "POST", body: JSON.stringify({ values: [row] }) });
+}
+export const SHEET_ID = "orders";
"""


def _counts(diff: str) -> tuple[int, int]:
    lines = [line for line in diff.splitlines() if not line.startswith(("+++", "---"))]
    return sum(line.startswith("+") for line in lines), sum(line.startswith("-") for line in lines)


class ChangesStub:
    ROOT = "/work/shop"
    BRANCH = "order-form"
    HEAD = "5c0ffee1a2b3c4d5e6f708192a3b4c5d6e7f8091"

    def __init__(self) -> None:
        self.diffs: dict[str, str] = {"order/form.js": FORM_DIFF, "order/sheet.js": SHEET_DIFF}
        self.files: list[dict[str, Any]] = []
        for path, status in (("order/form.js", "M"), ("order/sheet.js", "?")):
            added, removed = _counts(self.diffs[path])
            self.files.append({"path": path, "status": status, "added": added, "removed": removed, "binary": False})
        self.gone: dict[str, set[str]] = {}
        """What a chat reverted or committed: off its list from then on."""
        self.heads: dict[str, str] = {}
        self.writes: list[tuple[str, str, Any]] = []

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def rows(self, session_id: str) -> list[dict[str, Any]]:
        gone = self.gone.get(session_id, set())
        return [copy.deepcopy(f) for f in self.files if f["path"] not in gone]

    def listing(self, session_id: str) -> dict[str, Any]:
        files = self.rows(session_id)
        return {
            "env": "container", "directory": self.ROOT, "repository": True, "root": self.ROOT, "prefix": "", "branch": self.BRANCH,
            "head": self.heads.get(session_id, self.HEAD), "upstream": f"origin/{self.BRANCH}", "ahead": 1 if session_id in self.heads else 0, "behind": 0,
            "files": files, "added": sum(f["added"] or 0 for f in files), "removed": sum(f["removed"] or 0 for f in files),
            "truncated": False, "reason": "", "detail": "", "busy": "",
        }

    def answer(self, method: str, path: str, query: str, body: Any) -> tuple[int, str, str] | None:
        parts = path.split("/")
        method = method.upper()
        js = lambda status, payload: (status, "application/json", json.dumps(payload))  # noqa: E731
        if len(parts) < 5 or parts[2] != "sessions":
            return None
        session_id, rest = parts[3], parts[4:]
        payload = body if isinstance(body, dict) else {}
        if rest == ["changes"] and method == "GET":
            return js(200, self.listing(session_id))
        if rest == ["changes", "diff"] and method == "GET":
            wanted = (parse_qs(query).get("path") or [""])[0]
            if not wanted or wanted.startswith("/") or ".." in wanted.split("/"):
                return js(400, {"detail": "the path climbs out of the repository"})
            row = next((f for f in self.rows(session_id) if f["path"] == wanted), None)
            diff = self.diffs.get(wanted, "") if row is not None else ""
            return js(200, {"path": wanted, "diff": diff, "truncated": False, "untracked": bool(row and row["status"] == "?"), "binary": False, "reason": "", "detail": ""})
        if rest == ["changes", "revert"] and method == "POST":
            self.writes.append((method, path, payload))
            if payload.get("confirm") is not True:
                return js(400, {"detail": "a revert discards work that cannot be brought back; send confirm: true"})
            if bool(payload.get("all")) == bool(payload.get("paths")):
                return js(400, {"detail": "name the files to revert in paths, or send all: true, not both"})
            chosen = self._chosen(session_id, None if payload.get("all") else list(payload["paths"]))
            if isinstance(chosen, tuple):
                return js(*chosen)
            self.gone.setdefault(session_id, set()).update(f["path"] for f in chosen)
            return js(200, {"env": "container", "directory": self.ROOT, "root": self.ROOT, "branch": self.BRANCH, "reverted": [f["path"] for f in chosen],
                            "restored": [f["path"] for f in chosen if f["status"] not in ("?", "A")],
                            "deleted": sorted(f["path"] for f in chosen if f["status"] in ("?", "A"))})
        if rest == ["commit"] and method == "POST":
            self.writes.append((method, path, payload))
            message = str(payload.get("message") or "").strip()
            if not message:
                return js(400, {"detail": "a commit needs a message"})
            chosen = self._chosen(session_id, payload.get("paths"))
            if isinstance(chosen, tuple):
                return js(*chosen)
            self.gone.setdefault(session_id, set()).update(f["path"] for f in chosen)
            head = hashlib.sha1(f"{session_id}:{message}:{len(self.writes)}".encode()).hexdigest()
            self.heads[session_id] = head
            return js(200, {"env": "container", "directory": self.ROOT, "root": self.ROOT, "branch": self.BRANCH, "commit": head, "files": [f["path"] for f in chosen]})
        return None

    def _chosen(self, session_id: str, paths: list[str] | None) -> list[dict[str, Any]] | tuple[int, dict[str, str]]:
        rows = self.rows(session_id)
        if paths is None:
            chosen = rows
        else:
            by_path = {f["path"]: f for f in rows}
            missing = next((p for p in paths if p not in by_path), None)
            if missing is not None:
                return 400, {"detail": f"{missing} is not a changed file of this folder"}
            chosen = [by_path[p] for p in dict.fromkeys(paths)]
        if not chosen:
            return 409, {"detail": "nothing to do: the folder has no changes"}
        return chosen


DEFAULT = ChangesStub()
