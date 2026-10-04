"""File tools: read, write, edit, find, search."""

from __future__ import annotations

import asyncio
import difflib
import shlex
from pathlib import Path
from typing import Any

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.host.services import PathOutsideProject
from daedalus.processes import end_tree
from daedalus.stores.control import ControlConflict, ControlDenied
from daedalus.tools import search_hint
from daedalus.tools._common import FRAME_CHARS, clip, error, ok, output_limit, refuse_protected, services_for
from daedalus.tools.shell import shell_argv, shell_environment

_MAX_LINE_CHARS = 2000
#: How far into a file to look for a NUL before calling it binary. A text file
#: does not carry one; a compiled object, an archive or an image carries one
#: early, and 8 KiB is past every plausible text header.
_BINARY_SNIFF_CHARS = 8192


def _resolve_path(context: ToolContext, services: Any, path: str | None, action: str) -> tuple[Path | None, ToolResult | None]:
    candidate = Path(path or ".").expanduser()
    if not candidate.is_absolute():
        candidate = services.workspace_dir / candidate
    if refusal := refuse_protected(context, services, candidate, action):
        return None, refusal
    try:
        # A writer is held to the folders the session may write, so a folder marked read-only is
        # refused to Write and Edit while Read, Find and Search still see it.
        return services.resolve(path, write=action in ("written", "edited")), None
    except PathOutsideProject as exc:
        return None, error(context, str(exc))


def _line_allowance(text: str, lines: int, budget: int) -> int:
    """How many lines of this file fit in one call's budget.

    ``limit`` is the model's ask and it is often a round number chosen without
    knowing the file — 400 lines of a minified bundle is megabytes. The ask is
    honoured up to what the budget can actually carry, measured on this file's
    own average line, so a call returns a window the model can read rather than
    a clip in the middle of one.
    """
    average = max(1, len(text) // max(lines, 1)) + 8  # + the line-number column
    return max(1, budget // min(average, _MAX_LINE_CHARS + 8))


@search_hint(
    "read open view show file contents cat lines config source "
    "прочитать прочитай открыть открой показать покажи содержимое файла конфиг глянуть глянь"
)
@tool(
    name="Read",
    description=(
        "Read a text file. Returns numbered lines. Use offset (1-based line) and limit to page "
        "through large files. Relative paths resolve against the session workspace."
    ),
)
async def read_file(
    context: ToolContext, path: str, offset: int = 1, limit: int = 400
) -> ToolResult:
    services = services_for(context)
    if path.startswith("att:"):
        from daedalus.stores.files import FileRefused  # Lazy: attachment reads alone need the file store.
        from daedalus.tools.vision import VisionUnavailable, kept_body  # Lazy: text reads avoid vision imports.

        manager = services.extra.get("manager")
        if manager is None:
            return error(context, "kept files are unavailable in this session")
        owner = await manager.db.fetchone("SELECT project_id FROM sessions WHERE id=?", (services.session_id,))
        if owner is None or not owner["project_id"]:
            return error(context, "this session has no project file scope")
        try:
            stored = await manager.files.in_scope(path, owner["project_id"])
            data = await manager.files.read(stored)
            body = await kept_body(manager, data, stored, offset=offset, limit=limit)
        except (FileRefused, VisionUnavailable, OSError, ValueError) as exc:
            return error(context, str(exc))
        return ok(context, f"{stored.handle} {stored.name} ({stored.mime}, {stored.size} bytes):\n{body}")
    fs = services.fs
    target, refusal = _resolve_path(context, services, path, "read")
    if refusal is not None:
        return refusal
    assert target is not None
    if not await fs.exists(target):
        return error(context, f"no such file: {target}")
    if await fs.is_dir(target):
        entries = await fs.listdir(target)
        return ok(context, f"{target} is a directory with {len(entries)} entries:\n" + "\n".join(entries[:500]))
    size = await fs.size(target)
    try:
        text = await fs.read_text(target)
    except UnicodeDecodeError:
        return error(context, _binary_refusal(target, size))
    if "\x00" in text[:_BINARY_SNIFF_CHARS]:
        return error(context, _binary_refusal(target, size))
    budget = output_limit(context)
    lines = text.splitlines()
    start = max(offset, 1) - 1
    window = lines[start : start + max(1, min(max(limit, 1), _line_allowance(text, len(lines), budget)))]
    numbered = "\n".join(
        f"{start + i + 1:>6}\t{line[:_MAX_LINE_CHARS]}" for i, line in enumerate(window)
    )
    header = ""
    # The budget is in characters, so the file is measured in characters too:
    # `size` is bytes, and for a file that is mostly not Latin the two differ by
    # a factor that would fire this header on a file comfortably under budget.
    if len(text) > budget:
        # The model asked for a file it cannot be shown whole. Say so before
        # the lines, not after: a slice read as the whole file is how a wrong
        # answer gets written confidently.
        if len(lines) <= 1:
            header = (
                f"[{target}: {len(text)} characters on a single line — one call shows at most "
                f"{_MAX_LINE_CHARS} characters of it; use Exec with cut/sed, or Grep, for the rest]\n"
            )
        else:
            header = (
                f"[{target}: {len(text)} characters, {len(lines)} lines — showing lines "
                f"{start + 1}-{start + len(window)} of them]\n"
            )
    footer = ""
    if start + len(window) < len(lines):
        footer = f"\n[{len(lines) - start - len(window)} more lines; continue with offset={start + len(window) + 1}]"
    body = clip(numbered, max(budget - FRAME_CHARS, FRAME_CHARS))
    return ok(context, header + body + footer, total_lines=len(lines))


def _binary_refusal(target: Path, size: int) -> str:
    return (
        f"{target} is binary ({size} bytes) and would be unreadable as text. "
        "Use ImageView if it is an image, or Exec with file/xxd/strings to look at it."
    )


@search_hint(
    "write create new file save overwrite put content "
    "записать запиши создать создай новый файл сохранить сохрани перезаписать перезапиши"
)
@tool(
    name="Write",
    description="Create or overwrite a text file with the given content. Parent directories are created.",
)
async def write_file(context: ToolContext, path: str, content: str) -> ToolResult:
    services = services_for(context)
    target, refusal = _resolve_path(context, services, path, "written")
    if refusal is not None:
        return refusal
    assert target is not None
    try:
        await services.fs.write_text(target, content)
    except (OSError, ControlConflict, ControlDenied) as exc:
        return error(context, f"could not write {target}: {exc}")
    return ok(context, f"wrote {len(content)} characters to {target}" + await diagnostics(services, target), path=str(target))


@search_hint(
    "edit change replace fix typo modify patch line update value version string "
    "поменять поменяй заменить замени исправить исправь поправить поправь изменить измени опечатка правка обновить значение"
)
@tool(
    name="Edit",
    description=(
        "Replace a substring in a text file. old_string must match once unless replace_all is true; an "
        "exact match is tried first, then one ignoring trailing whitespace, then one ignoring indentation "
        "(the file's indentation is kept). On a miss the error shows the closest lines. For several "
        "changes to one file use MultiEdit."
    ),
)
async def edit_file(
    context: ToolContext, path: str, old_string: str, new_string: str, replace_all: bool = False
) -> ToolResult:
    services = services_for(context)
    target, refusal = _resolve_path(context, services, path, "edited")
    if refusal is not None:
        return refusal
    assert target is not None
    fs = services.fs
    if not await fs.is_file(target):
        return error(context, f"no such file: {target}")
    text = await fs.read_text(target)
    try:
        updated, count, how = apply_edit(text, old_string, new_string, replace_all=replace_all)
    except EditMiss as miss:
        return error(context, str(miss))
    try:
        await fs.write_text(target, updated)
    except (OSError, ControlConflict, ControlDenied) as exc:
        return error(context, f"could not write {target}: {exc}")
    note = f" (matched {how})" if how != "exactly" else ""
    return ok(context, f"edited {target}: {count} replacement(s){note}" + await diagnostics(services, target), path=str(target))


class EditMiss(ValueError):
    """The old text is not in the file; the message shows the nearest lines so the next attempt can be exact."""


def _loose_hits(text: str, old: str) -> tuple[list[int], int, str]:
    """Line indices where ``old`` occurs ignoring trailing whitespace, then ignoring indentation: ``(hits, n_lines, how)``."""
    lines = text.splitlines(keepends=True)
    old_lines = old.splitlines()
    if not old_lines:
        raise EditMiss("old_string is empty")
    for how, norm in (("ignoring trailing whitespace", lambda x: x.rstrip()), ("ignoring indentation", lambda x: x.strip())):
        wanted = [norm(line) for line in old_lines]
        hits = [i for i in range(len(lines) - len(wanted) + 1) if [norm(line.rstrip("\r\n")) for line in lines[i : i + len(wanted)]] == wanted]
        if hits:
            return hits, len(wanted), how
    raise EditMiss("old_string not found in file" + nearest_window(text, old))


NEAREST_MAX_LINES = 40_000
"""Files longer than this get no "closest lines" hint: scoring windows of a huge file would stall the turn."""


def nearest_window(text: str, old: str, *, context_lines: int = 2) -> str:
    """The lines of the file that look most like ``old``, numbered, so the caller sees what is actually there.

    Bounded: candidate windows are the lines closest to ``old``'s first line (a cheap per-line ratio), and only
    the best few are scored as whole windows.
    """
    lines = text.splitlines()
    old_lines = old.splitlines() or [old]
    size = max(1, len(old_lines))
    if not lines or len(lines) > NEAREST_MAX_LINES:
        return ""
    first = old_lines[0].strip()
    matcher = difflib.SequenceMatcher(None, "", first)
    scored: list[tuple[float, int]] = []
    for i, line in enumerate(lines[: max(1, len(lines) - size + 1)]):
        matcher.set_seq1(line.strip())
        if matcher.real_quick_ratio() < 0.5 or matcher.quick_ratio() < 0.5:
            continue
        scored.append((matcher.quick_ratio(), i))
    scored.sort(reverse=True)
    best, best_at = 0.0, 0
    for _, i in scored[:12]:
        ratio = difflib.SequenceMatcher(None, "\n".join(lines[i : i + size]), old).ratio()
        if ratio > best:
            best, best_at = ratio, i
    if best < 0.3:
        return ""
    start = max(0, best_at - context_lines)
    end = min(len(lines), best_at + size + context_lines)
    shown = "\n".join(f"{n + 1:>6}\t{lines[n][:_MAX_LINE_CHARS]}" for n in range(start, end))
    return f". The closest lines ({best:.0%} similar):\n{shown}"


def _reindent(new: str, old: str, block: str) -> str:
    """The replacement carried to the indentation the file actually has at the matched block."""
    indent = block[: len(block) - len(block.lstrip())]
    old_indent = old[: len(old) - len(old.lstrip())]
    return "\n".join((indent + line[len(old_indent):]) if line.startswith(old_indent) and line.strip() else line for line in new.split("\n"))


def apply_edit(text: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, int, str]:
    """``(updated_text, replacements, how)``; ``how`` says which matching level found the text and, for a loose
    match, at which line."""
    count = text.count(old)
    if count:
        if count > 1 and not replace_all:
            raise EditMiss(f"old_string matches {count} times; make it unique or set replace_all")
        return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), (count if replace_all else 1), "exactly"
    hits, n, how = _loose_hits(text, old)
    kept: list[int] = []
    for h in hits:  # overlapping windows would edit the same lines twice
        if not kept or h >= kept[-1] + n:
            kept.append(h)
    hits = kept
    if len(hits) > 1 and not replace_all:
        raise EditMiss(f"old_string matches {len(hits)} times when {how} (lines {', '.join(str(h + 1) for h in hits[:8])}); make it unique or set replace_all")
    lines = text.splitlines(keepends=True)
    chosen = hits if replace_all else hits[:1]
    for i in reversed(chosen):  # from the bottom, so earlier indices stay valid
        block = "".join(lines[i : i + n])
        ending = "\r\n" if block.endswith("\r\n") else ("\n" if block.endswith("\n") else "")
        body = block[: len(block) - len(ending)] if ending else block
        replacement = _reindent(new, old, body) if how == "ignoring indentation" else new
        lines[i : i + n] = [replacement + ending]
    return "".join(lines), len(chosen), f"{how} at line {hits[0] + 1}" + (f" and {len(chosen) - 1} more" if len(chosen) > 1 else "")


@search_hint(
    "several edits many changes one file batch replace atomic multiple replacements "
    "несколько правок много замен одном файле пачкой внести внеси правки сразу"
)
@tool(
    name="MultiEdit",
    description=(
        "Apply several edits to one file atomically: edits is a list of {old_string, new_string[, replace_all]} "
        "applied in order, each seeing the result of the previous ones; if any edit fails the file is left unchanged and the error names it."
    ),
)
async def multi_edit(context: ToolContext, path: str, edits: list[dict[str, Any]]) -> ToolResult:
    services = services_for(context)
    target, refusal = _resolve_path(context, services, path, "edited")
    if refusal is not None:
        return refusal
    assert target is not None
    fs = services.fs
    if not await fs.is_file(target):
        return error(context, f"no such file: {target}")
    if not edits:
        return error(context, "edits is empty")
    text = await fs.read_text(target)
    total = 0
    for index, edit in enumerate(edits, 1):
        old, new = str(edit.get("old_string", "")), str(edit.get("new_string", ""))
        try:
            text, count, _ = apply_edit(text, old, new, replace_all=bool(edit.get("replace_all", False)))
        except EditMiss as miss:
            return error(context, f"edit {index} of {len(edits)} failed, file unchanged: {miss}")
        total += count
    try:
        await fs.write_text(target, text)
    except (OSError, ControlConflict, ControlDenied) as exc:
        return error(context, f"could not write {target}: {exc}")
    return ok(context, f"edited {target}: {total} replacement(s) in {len(edits)} edits" + await diagnostics(services, target), path=str(target))


_DIAGNOSABLE = {".py"}
DIAGNOSTICS_TIMEOUT = 20.0


async def diagnostics(services: Any, target: Path) -> str:
    """A syntax check of the file just written, appended to the tool result: the model sees a broken file at once.

    Python only for now (compile, then ruff when it is installed); nothing for other types. The check runs
    where the file lives — through the exec backend when the session drives another machine.
    """
    if target.suffix not in _DIAGNOSABLE:
        return ""
    quoted = shlex.quote(str(target))
    command = (
        f"python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding=\"utf-8\").read(), sys.argv[1])' {quoted}"
        f" && (command -v ruff >/dev/null 2>&1 && ruff check --output-format concise --no-cache {quoted} || true)"
    )
    try:
        if services.exec_backend is not None:
            outcome = await services.exec_backend.run(command, cwd=None, env=None, timeout=DIAGNOSTICS_TIMEOUT)
            code, out = outcome.exit_code, outcome.output
        else:
            proc = await asyncio.create_subprocess_exec(*shell_argv(command), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=shell_environment(services.session_id), start_new_session=True)
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=DIAGNOSTICS_TIMEOUT)
            except TimeoutError:
                try:
                    end_tree(proc.pid, hard=True)
                except ProcessLookupError:
                    pass
                await proc.wait()
                return ""
            code, out = proc.returncode or 0, raw.decode("utf-8", "replace")
    except (OSError, TimeoutError):
        return ""
    out = out.strip()
    if code != 0:
        return f"\n⚠ the file does not compile:\n{out[:1500]}"
    if out and "All checks passed" not in out:
        return f"\nlint:\n{out[:1500]}"
    return ""


@search_hint(
    "find files by name glob pattern where is locate path which files exist "
    "найти найди файлы имени маске где лежит где находится путь расположение"
)
@tool(
    name="Find",
    description="Find files by glob pattern (e.g. '**/*.py') under a directory (default: workspace).",
)
async def find_files(
    context: ToolContext, pattern: str = "**/*", path: str | None = None, limit: int = 500
) -> ToolResult:
    services = services_for(context)
    root, refusal = _resolve_path(context, services, path, "listed")
    if refusal is not None:
        return refusal
    assert root is not None
    if not await services.fs.is_dir(root):
        return error(context, f"not a directory: {root}")
    matches = await services.fs.find(root, pattern, limit)
    return ok(context, "\n".join(matches) or "(no matches)", count=len(matches))


@search_hint(
    "grep ripgrep search code contents regex occurrences usages text inside files where implemented "
    "грепнуть грепни поиск по коду вхождения использования где используется искать ищи где реализовано"
)
@tool(
    name="Search",
    description=(
        "Search file contents with ripgrep (regex). Returns 'path:line: text' lines. "
        "Use glob to restrict file types, e.g. '*.py'."
    ),
)
async def search_files(
    context: ToolContext,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    case_insensitive: bool = False,
    limit: int = 200,
) -> ToolResult:
    services = services_for(context)
    root, refusal = _resolve_path(context, services, path, "searched")
    if refusal is not None:
        return refusal
    assert root is not None
    code, text, err = await services.fs.search(root, pattern, glob=glob, case_insensitive=case_insensitive, limit=limit)
    if code == 1:
        return ok(context, "(no matches)", count=0)
    if code not in (0, 1):
        return error(context, err or f"search exited {code}")
    lines = text.splitlines()[:limit]
    rel = [line.replace(str(root) + "/", "", 1) for line in lines]
    return ok(context, clip("\n".join(rel), services.max_tool_output_chars), count=len(rel))


TOOLS = [read_file, write_file, edit_file, multi_edit, find_files, search_files]

__all__ = ["TOOLS"]
