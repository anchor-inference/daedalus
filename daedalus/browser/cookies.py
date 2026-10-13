"""Cookies handed to the agent in a file, read into what the browser daemon sets.

The operator exports a site's cookies from their own browser (an extension's ``cookies.txt``, a
Cookie-Editor or EditThisCookie JSON, Playwright's ``storageState``, a copied ``Cookie:`` header) and
attaches the file, so the agent's browser is signed in where they are. Every shape becomes the
DevTools protocol's ``Network.CookieParam``.

A value is read here and passed to the daemon, and goes nowhere else: what this module says about a
file — the summary, a line it skipped and why — names domains, names and line numbers, never a value,
because whatever it says reaches the model's context and the transcript.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MAX_BYTES = 1_000_000
"""The largest file read as cookies: a real export is a few kilobytes; past this it is not a jar."""

NETSCAPE_HEADERS = ("# netscape http cookie file", "# http cookie file")
SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}
"""How the exports spell ``sameSite``: Chrome's extension API says ``no_restriction``, ``lax``,
``strict`` and ``unspecified``; Playwright and the protocol say ``Strict``, ``Lax``, ``None``."""
TRUE_WORDS = {"true", "1", "yes", "✓"}
COOKIE_NAME = re.compile(r"^[^\s;,=]+$")


@dataclass(slots=True)
class Parsed:
    format: str
    """``netscape``, ``json`` or ``header``."""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """Each skipped entry as ``line 4: expired`` or ``item 2 (sid): no domain`` — never its value."""
    notes: list[str] = field(default_factory=list)

    def domains(self) -> Counter[str]:
        return Counter(cookie_site(c) for c in self.cookies)


class CookieFileError(ValueError):
    """The text is no cookie file the reader knows; the message says what it expected."""


def cookie_site(cookie: dict[str, Any]) -> str:
    """The site a parameter is for, as it is summarised: its domain without the leading dot."""
    domain = str(cookie.get("domain") or "")
    if not domain:
        url = str(cookie.get("url") or "")
        domain = re.sub(r"^https?://", "", url).split("/", 1)[0].split(":", 1)[0]
    return domain.lstrip(".").lower()


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_WORDS


def _expiry(value: Any) -> float | None:
    """Seconds since the epoch, or ``None`` for a session cookie: 0 and -1 mean one in every export,
    a date string is the DevTools table's, ``Session`` its word for none."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if text.lower() in ("session", "0", "-1"):
            return None
        try:
            seconds = float(text)
        except ValueError:
            try:
                seconds = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                raise ValueError(f"expiry {text[:40]!r} is not a time") from None
    if seconds <= 0:
        return None
    if seconds > 1e11:
        seconds /= 1000  # milliseconds, as some exports write them
    return seconds


def _param(name: str, value: str, *, domain: str, host_only: bool, path: str, secure: bool, http_only: bool, same_site: str | None, expires: float | None, now: float) -> dict[str, Any]:
    """One ``Network.CookieParam``, or ``ValueError`` saying why the cookie cannot be one."""
    domain = domain.strip().lower()
    if not domain.lstrip("."):
        raise ValueError("no domain")
    if not name or not COOKIE_NAME.match(name):
        raise ValueError("no name, or a name a browser does not accept")
    if expires is not None and expires < now:
        raise ValueError("expired")
    param: dict[str, Any] = {"name": name, "value": value, "path": path or "/"}
    if host_only and not domain.startswith("."):
        # Only an address makes a host-only cookie: a domain, given to the browser, is a domain
        # cookie that every subdomain is sent too.
        param["url"] = ("https://" if secure else "http://") + domain + (path or "/")
    else:
        param["domain"] = "." + domain.lstrip(".")
    if secure:
        param["secure"] = True
    if http_only:
        param["httpOnly"] = True
    site = SAME_SITE.get((same_site or "").strip().lower())
    # SameSite=None without Secure is refused by the browser outright; left out, it is Lax, which is
    # what the site got from the operator's browser for the same cookie.
    if site and not (site == "None" and not secure):
        param["sameSite"] = site
    if expires is not None:
        param["expires"] = expires
    return param


def parse_netscape(text: str, now: float) -> Parsed:
    out = Parsed("netscape")
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        http_only = False
        if line.startswith("#HttpOnly_"):
            # curl's and the extensions' mark for an HttpOnly cookie, on a line that is otherwise a comment.
            line, http_only = line[len("#HttpOnly_"):], True
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields, joiner = line.split("\t"), "\t"
        if len(fields) < 6:
            # Some copies turn the tabs into spaces; the fixed fields hold none, so they still split.
            fields, joiner = line.split(), " "
        if len(fields) == 6:
            fields.append("")  # an empty value, its tab trimmed away
        if len(fields) < 7 or fields[1].upper() not in ("TRUE", "FALSE") or fields[3].upper() not in ("TRUE", "FALSE"):
            out.skipped.append(f"line {number}: not the seven tab-separated fields of a cookies.txt line")
            continue
        domain, subdomains, path, secure, expiry, name = fields[:6]
        value = joiner.join(fields[6:])
        try:
            out.cookies.append(_param(
                name, value, domain=domain, host_only=not _flag(subdomains) and not domain.startswith("."), path=path, secure=_flag(secure), http_only=http_only,
                same_site=None, expires=_expiry(expiry), now=now,
            ))
        except ValueError as exc:
            out.skipped.append(f"line {number} ({name[:60]}): {exc}")
    return out


def _json_cookies(data: Any) -> tuple[list[Any], list[str]]:
    notes: list[str] = []
    if isinstance(data, dict):
        if isinstance(data.get("origins"), list) and data["origins"]:
            notes.append("the file's localStorage (origins) is not imported, only its cookies")
        data = data.get("cookies")
    if not isinstance(data, list):
        raise CookieFileError("JSON cookies are a list of cookie objects, or an object with a cookies list (Playwright's storageState)")
    return data, notes


def parse_json(text: str, now: float) -> Parsed:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieFileError(f"not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    items, notes = _json_cookies(data)
    out = Parsed("json", notes=notes)
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            out.skipped.append(f"item {number}: not a cookie object")
            continue
        name = str(item.get("name") or "")
        try:
            if "value" not in item:
                raise ValueError("no value")
            domain = str(item.get("domain") or "")
            if not domain and item.get("url"):
                domain = re.sub(r"^https?://", "", str(item["url"])).split("/", 1)[0].split(":", 1)[0]
            # Cookie-Editor and EditThisCookie say hostOnly; Playwright and the protocol mark a
            # domain cookie by its leading dot alone.
            host_only = _flag(item["hostOnly"]) if "hostOnly" in item else not domain.startswith(".")
            session = _flag(item.get("session")) if "session" in item else False
            expires = None if session else _expiry(item.get("expirationDate", item.get("expires")))
            out.cookies.append(_param(
                name, str(item.get("value") or ""), domain=domain, host_only=host_only, path=str(item.get("path") or "/"), secure=_flag(item.get("secure", False)),
                http_only=_flag(item.get("httpOnly", False)), same_site=str(item.get("sameSite") or ""), expires=expires, now=now,
            ))
        except ValueError as exc:
            out.skipped.append(f"item {number}{f' ({name[:60]})' if name else ''}: {exc}")
    return out


def parse_header(text: str, domain: str | None, now: float) -> Parsed:
    """A ``Cookie:`` request header, or its value alone: names and values, for the site in ``domain``."""
    body = text.strip()
    if body.lower().startswith("cookie:"):
        body = body[len("cookie:"):]
    if not domain or not domain.strip().strip("."):
        raise CookieFileError("a Cookie header does not say which site it is for; give domain (wolt.com)")
    site = re.sub(r"^https?://", "", domain.strip()).split("/", 1)[0].lstrip(".")
    out = Parsed("header")
    for number, pair in enumerate(body.split(";"), 1):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        try:
            if not sep:
                raise ValueError("not a name=value pair")
            # A domain cookie, so www. and the bare site both get it: a header says nothing of where
            # the cookie was set, and the operator means the site.
            out.cookies.append(_param(name.strip(), value.strip(), domain=site, host_only=False, path="/", secure=True, http_only=False, same_site=None, expires=None, now=now))
        except ValueError as exc:
            out.skipped.append(f"pair {number}{f' ({name.strip()[:60]})' if sep else ''}: {exc}")
    return out


def looks_netscape(text: str) -> bool:
    head = text.lstrip().lower()
    if head.startswith(NETSCAPE_HEADERS):
        return True
    lines = [line for line in text.splitlines() if line.strip() and (not line.startswith("#") or line.startswith("#HttpOnly_"))]
    return bool(lines) and all(len(line.split("\t")) in (6, 7) for line in lines[:20])


def parse(data: bytes | str, *, domain: str | None = None, now: float | None = None) -> Parsed:
    """Read cookies in any shape the module knows; ``CookieFileError`` when it is none of them."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            raise CookieFileError(f"the file is {len(data)} bytes; a cookie file is at most {MAX_BYTES}")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CookieFileError("the file is not text") from None
    else:
        text = data.lstrip("﻿")
    moment = time.time() if now is None else now
    stripped = text.strip()
    if not stripped:
        raise CookieFileError("the file is empty")
    if stripped[0] in "[{":
        return parse_json(stripped, moment)
    if looks_netscape(text):
        return parse_netscape(text, moment)
    if "=" in stripped and "\n" not in stripped:
        return parse_header(stripped, domain, moment)
    raise CookieFileError("not a cookies.txt (Netscape) file, a JSON cookie export or a Cookie header")


def sniff(data: bytes, *, limit: int = 5) -> list[str] | None:
    """The sites a file holds cookies for, when it is a cookie file; ``None`` when it is not.

    For the line that names an attachment: a guess that must never be wrong the costly way, so only
    a Netscape file or a JSON export whose objects look like cookies counts, never a bare header —
    any text with an equals sign would.
    """
    if len(data) > MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        if stripped[0] in "[{":
            items, _ = _json_cookies(json.loads(stripped))
            if not items or not all(isinstance(i, dict) and "name" in i and "value" in i and ("domain" in i or "url" in i) for i in items):
                return None
            parsed = parse_json(stripped, time.time())
        elif looks_netscape(text):
            parsed = parse_netscape(text, time.time())
        else:
            return None
    except (CookieFileError, json.JSONDecodeError, ValueError):
        return None
    sites = [site for site, _ in parsed.domains().most_common()]
    if not sites:
        return None
    return sites[:limit] + ([f"{len(sites) - limit} more"] if len(sites) > limit else [])


def summary(parsed: Parsed) -> str:
    """The counts per site, in a line: ``wolt.com 12, auth.wolt.com 3``."""
    return ", ".join(f"{site} {n}" for site, n in sorted(parsed.domains().items(), key=lambda kv: (-kv[1], kv[0])))


__all__ = ["MAX_BYTES", "CookieFileError", "Parsed", "cookie_site", "parse", "sniff", "summary"]
