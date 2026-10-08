"""Tools that reach the chat surface: deliver files, spawn sessions."""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools import search_hint, tool_group
from daedalus.tools._common import error, local_copy, ok, refuse_protected, services_for


@search_hint(
    "send file to operator chat share document archive report deliver upload attach grab "
    "отправить отправь скинуть скинь прислать пришли приложить файл документ архив отчет чат"
)
@tool(
    name="SendFile",
    description=(
        "Send a file from the workspace (or any path) to the operator in the current chat, "
        "with an optional caption. Images (png/jpg) render inline; everything else arrives as a "
        "document. Use it for reports, archives, generated code and any output longer than a "
        "screen. Chat replies themselves are Markdown and render natively (headings, tables, "
        "code blocks, quotes, <details> blocks), so short structured answers need no file."
    ),
)
async def send_file(context: ToolContext, path: str, caption: str | None = None) -> ToolResult:
    services = services_for(context)
    target = services.resolve(path)
    if refusal := refuse_protected(context, services, target, "sent"):
        return refusal
    if services.send_file is None:
        return error(context, "file delivery is not available in this session")
    try:
        # A session working on the host names a file there; the chat is sent a copy fetched here.
        local = await local_copy(services, target)
    except FileNotFoundError:
        return error(context, f"no such file: {target}")
    except (OSError, ValueError) as exc:
        return error(context, str(exc))
    if not local.is_file():
        return error(context, f"no such file: {target}")
    result = await services.send_file(local, caption)
    return ok(context, f"sent {target.name} ({local.stat().st_size} bytes): {result}")


@search_hint(
    "attach picture photo image video audio gif album inline answer embed media "
    "прикрепить прикрепи вставить вставь картинку фото фотку видео гифку альбом медиа ответ"
)
@tool(
    name="AttachMedia",
    description=(
        "Attach images, a picture album, video, audio or a GIF inside the final answer. Each item is "
        "{path, alt, caption} for a workspace file, or {url, alt, caption, kind} for an http(s) link. "
        "Pass the link itself; do not download it first. kind is image, video, animation or audio, and is "
        "only needed when the link has no filename extension. Use layout=album for 2-10 images, videos or "
        "GIFs in any order, otherwise use single with one item. The result contains one Markdown line: "
        "copy that line into the final answer exactly where the media should appear. This prepares the "
        "media; do not also SendFile it."
    ),
)
async def attach_media(context: ToolContext, items: list[dict[str, str]], layout: str = "single") -> ToolResult:
    services = services_for(context)
    if services.attach_media is None:
        return error(context, "inline media is not available in this session")
    resolved: list[dict[str, str]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        raw = str(item.get("path") or "").strip()
        if url and raw:
            return error(context, "a media item is a file or a link, not both")
        if url:
            resolved.append({"url": url, "kind": str(item.get("kind") or ""), "alt": str(item.get("alt") or ""), "caption": str(item.get("caption") or "")})
            continue
        if not raw:
            return error(context, "every media item needs a path or a url")
        target = services.resolve(raw)
        if refusal := refuse_protected(context, services, target, "attached"):
            return refusal
        try:
            local = await local_copy(services, target)
        except FileNotFoundError:
            return error(context, f"no such file: {target}")
        except (OSError, ValueError) as exc:
            return error(context, str(exc))
        if not local.is_file():
            return error(context, f"no such file: {target}")
        resolved.append({"path": str(local), "alt": str(item.get("alt") or ""), "caption": str(item.get("caption") or "")})
    try:
        attached = await services.attach_media(resolved, layout)
    except (OSError, ValueError) as exc:
        return error(context, str(exc))
    return ok(context, f"media prepared ({attached['kind']}). Put this line in the final answer:\n\n{attached['markdown']}", presentation_id=attached["id"])


@tool_group("agents")
@search_hint(
    "spawn independent agent own session separate chat topic persistent worker brief peer "
    "создать создай отдельного агента завести заведи независимый агент своя сессия отдельный чат воркер"
)
@tool(
    name="SpawnAgent",
    description=(
        "Create an independent agent: a new session (its own chat topic and workspace) that keeps a brief "
        "you write in its system prompt, gets copies of the files it needs, and optionally a model "
        "preset, a mode, MCP servers, a peer name (so you can AskPeer it later), tools to withhold "
        "(tools_off), and a loop: loop_instruction makes it a loop agent woken up for that task every "
        "loop_interval_minutes, or at delays it picks itself when no interval is given. A first_message "
        "starts it on a one-off job at once. Use it when another "
        "agent should own a job for good and you hold what it needs (instructions, files, settings); "
        "for a piece of YOUR current task in your own workspace use SubAgent instead. The brief is the hand-over: what the agent is for, how the work "
        "is done, where things are, what to avoid. Paths are relative to this workspace or absolute."
    ),
)
async def spawn_agent(
    context: ToolContext,
    title: str,
    brief: str,
    files: list[str] | None = None,
    first_message: str | None = None,
    preset: str | None = None,
    mode: str | None = None,
    mcp: list[str] | None = None,
    peer_name: str | None = None,
    tools_off: list[str] | None = None,
    loop_instruction: str | None = None,
    loop_interval_minutes: int | None = None,
    loop_max_runs: int | None = None,
) -> ToolResult:
    services = services_for(context)
    if services.spawn_agent is None:
        return error(context, "spawning agents is not available here")
    if not brief.strip():
        return error(context, "the brief is what the new agent lives by; write it")
    loop = None
    if loop_instruction and loop_instruction.strip():
        loop = {"instruction": loop_instruction.strip(), "mode": "interval" if loop_interval_minutes else "dynamic", "interval_seconds": int(loop_interval_minutes) * 60 if loop_interval_minutes else None, "max_runs": loop_max_runs}
    paths: list[str] = []
    missing: list[str] = []
    for name in files or []:
        target = services.resolve(name)
        try:
            # The new agent's inbox is filled here, so a file on the host is fetched first.
            local = await local_copy(services, target)
        except FileNotFoundError:
            missing.append(str(target))
            continue
        except (OSError, ValueError) as exc:
            return error(context, str(exc))
        if not local.exists():
            missing.append(str(target))
        paths.append(str(local))
    if missing:
        return error(context, "these files do not exist: " + ", ".join(missing))
    try:
        session_id = await services.spawn_agent(
            title=title, brief=brief, files=paths, first_message=first_message, preset=preset, mode=mode, mcp=list(mcp or []), peer_name=peer_name, tools_off=list(tools_off or []), loop=loop
        )
    except (ValueError, KeyError) as exc:
        return error(context, str(exc))
    extras = (f", peer '{peer_name}'" if peer_name else "") + (f", {len(paths)} file(s) copied to its inbox" if paths else "") + (f", {len(tools_off)} tool(s) withheld" if tools_off else "")
    if loop:
        extras += f", loop {'every ' + str(loop_interval_minutes) + ' min' if loop_interval_minutes else 'dynamically paced'}"
    return ok(context, f"agent '{title}' created as session {session_id}" + extras, session_id=session_id)


TOOLS = [send_file, attach_media, spawn_agent]

__all__ = ["TOOLS"]
