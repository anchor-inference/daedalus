"""More than one account per subscription: where each one's login lives, and who it says it is.

The bot names the account a request is for in ``x-daedalus-account``; no header, or ``default``, is
the login the proxy has always served (the CLI's own ``~/.codex``, ``~/.grok``, ``~/.claude``, or the
plan's key in the keys file). Every other account is one of two things:

- a CLI login kept in a home of its own, ``<the CLI's folder>/daedalus-accounts/<id>``, which is the
  folder the CLI is pointed at with ``CODEX_HOME``, ``GROK_HOME`` or ``CLAUDE_CONFIG_DIR`` when the
  operator signs that account in. It sits inside the folder this process already reads (the compose
  stack mounts those three folders, a native installation reads them in place), so a new account
  needs no new mount on any system, and the operator's own CLI login next to it is never touched;
- a plan key (OpenCode Go, Z.AI Coding Plan …) under ``<the plan's variable>__<ID>`` in the keys file.

The name ``daedalus-accounts`` and not ``accounts``: an account switcher for the Codex CLI already
keeps an ``accounts`` folder in ``~/.codex``, and two programs writing one folder would each see the
other's logins as their own.

Nothing here reads a token out to anyone. What an account says about itself — its e-mail and its
plan — is read from what the CLI stored beside the token, without a network call.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path
from typing import Any

ACCOUNT_HEADER = "x-daedalus-account"
"""The request header that names the account; it never travels on to an upstream."""
DEFAULT_ACCOUNT = "default"
ACCOUNTS_DIR = "daedalus-accounts"
ACCOUNT_ID = re.compile(r"[a-z0-9]{1,32}")
"""Lower-case letters and digits: the id becomes a folder name and an environment variable's suffix, and
nothing else in it could reach outside the folder or be read as a second variable."""
LOGIN_UPSTREAMS = ("codex", "grok", "claude")
KEYED_PLANS = ("opencode", "zai_coding", "minimax_plan", "kimi_coding")
"""The prepaid plans that sign with a key; an account of one is a second key for the same plan."""


class AccountRefused(ValueError):
    """A request that names an account this proxy cannot serve, with the status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def account_id(raw: str) -> str:
    """The account a header or a path names, as stored: "" for the default login.

    Raises :class:`AccountRefused` for a name that is not an id, so a stray header cannot quietly fall
    back to the default login and spend the account the operator meant to spare."""
    value = (raw or "").strip().lower()
    if not value or value == DEFAULT_ACCOUNT:
        return ""
    if not ACCOUNT_ID.fullmatch(value):
        raise AccountRefused(400, f"{raw!r} is not an account id")
    return value


def account_home(default_login: Path, account: str) -> Path:
    """The folder an account's CLI is pointed at; the default login's own folder for the default."""
    return default_login.parent / ACCOUNTS_DIR / account if account else default_login.parent


def login_path(default_login: Path, account: str) -> Path:
    """Where the account's credentials file is: the same file name as the default login's, in its home."""
    return account_home(default_login, account) / default_login.name


def listed_logins(default_login: Path) -> list[str]:
    """The accounts with a home of their own under this CLI's folder, signed in or not."""
    root = default_login.parent / ACCOUNTS_DIR
    try:
        return sorted(entry.name for entry in root.iterdir() if entry.is_dir() and ACCOUNT_ID.fullmatch(entry.name))
    except OSError:
        return []


def prepare_login(default_login: Path, account: str) -> Path:
    """Make the account's home, private to its owner, before its CLI signs in there.

    The Codex CLI refuses a ``CODEX_HOME`` that does not exist yet, so the folder has to be there before
    the terminal opens; the other two would make it themselves."""
    home = account_home(default_login, account)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


def forget_login(default_login: Path, account: str) -> bool:
    """Remove an account's home and everything its CLI kept there; never the default login's folder."""
    if not account:
        raise AccountRefused(400, "the default login is the CLI's own; sign it out with the CLI")
    home = account_home(default_login, account)
    if home.parent.name != ACCOUNTS_DIR or not home.is_dir():
        return False
    shutil.rmtree(home)
    return True


def key_variable(plan_variable: str, account: str) -> str:
    """The keys-file variable that holds an account's key for a plan: the plan's own for the default."""
    return f"{plan_variable}__{account.upper()}" if account else plan_variable


def listed_keys(plan_variable: str, values: dict[str, str]) -> list[str]:
    """The accounts that have a key written for a plan."""
    prefix = plan_variable + "__"
    out = []
    for name, value in values.items():
        if name.startswith(prefix) and value:
            suffix = name[len(prefix):].lower()
            if ACCOUNT_ID.fullmatch(suffix):
                out.append(suffix)
    return sorted(out)


def _claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def identity(upstream: str, credentials: Path) -> dict[str, str]:
    """``{"email", "plan"}`` as far as the stored login says them; empty strings where it does not.

    - Codex keeps the ChatGPT id token, whose claims carry the address and the plan type.
    - Claude Code keeps the plan beside its token (``subscriptionType``) and the address in the
      ``.claude.json`` of its config folder, which for an account of its own is the account's home.
    - Grok keeps one session per principal; the first is the one served, and its token's claims carry
      the address when the issuer put it there.
    """
    data = _read(credentials)
    email = plan = ""
    if upstream == "codex" and isinstance(data, dict):
        claims = _claims(str((data.get("tokens") or {}).get("id_token") or ""))
        email = str(claims.get("email") or "")
        auth = claims.get("https://api.openai.com/auth")
        plan = str(auth.get("chatgpt_plan_type") or "") if isinstance(auth, dict) else ""
    elif upstream == "claude" and isinstance(data, dict):
        plan = str((data.get("claudeAiOauth") or {}).get("subscriptionType") or "")
        for candidate in (credentials.parent / ".claude.json", credentials.parent.parent / ".claude.json"):
            profile = _read(candidate)
            account = profile.get("oauthAccount") if isinstance(profile, dict) else None
            if isinstance(account, dict) and account.get("emailAddress"):
                email = str(account["emailAddress"])
                break
    elif upstream == "grok" and isinstance(data, dict) and data:
        entry = next(iter(data.values()))
        if isinstance(entry, dict):
            email = str(entry.get("email") or _claims(str(entry.get("key") or "")).get("email") or "")
    return {"email": email[:200], "plan": plan[:80]}


__all__ = [
    "ACCOUNTS_DIR",
    "ACCOUNT_HEADER",
    "ACCOUNT_ID",
    "DEFAULT_ACCOUNT",
    "KEYED_PLANS",
    "LOGIN_UPSTREAMS",
    "AccountRefused",
    "account_home",
    "account_id",
    "forget_login",
    "identity",
    "key_variable",
    "listed_keys",
    "listed_logins",
    "login_path",
    "prepare_login",
]
