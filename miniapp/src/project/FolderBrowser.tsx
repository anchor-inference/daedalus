// The folder browser: one directory level at a time, in the container or on the machine.
//
// It is a list, not a cloud of buttons: each folder says on its own line whether it is a repository,
// whether agents could write in it, when it changed and which project already owns it, so the
// operator can judge a folder before choosing it instead of after the project fails to start. A
// chat's scratch folder is shown under the chat's title and folded away with the installation's own
// folders: neither can be chosen, and a column of hashes was the old picker's whole content.
//
// The host side is read through the terminal daemon. When that daemon is stopped, missing or older
// than this version, the list says which, with the command that fixes it, and still lets the path
// be typed: the operator should never be stuck behind a component they did not ask about.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError, type ProjectEnvironments } from "../api";
import { relTime } from "../format";
import { t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { errorText } from "../ui";
import { pathProblem } from "../folders";

export type Env = "container" | "host";

export type FolderOwner = { id: string; name: string };

export type FolderEntry = {
  name: string;
  path: string;
  mtime: number;
  writable: boolean | null;
  readable: boolean;
  is_git: boolean;
  link?: boolean;
  kind: "folder" | "chat" | "service";
  project: FolderOwner | null;
  chat_title?: string;
};

type Place = { name: string; path: string; kind?: string; env?: Env };

export type Listing = {
  env: Env;
  path: string;
  parent: string | null;
  home: string;
  writable: boolean | null;
  is_git: boolean;
  crumbs: { name: string; path: string }[];
  entries: FolderEntry[];
  truncated: boolean;
  roots: Place[];
  recent: Place[];
  favourites: Place[];
  here: Pick<FolderEntry, "kind" | "project" | "chat_title">;
};

type Failure = { code: string; message: string; configured?: boolean; start?: string; install?: string; mount?: boolean; on_host?: boolean | null };

/** What the dialog is told about the chosen folder. `problem` is empty when the folder can be a new
 *  project's as it is; otherwise it names why not, and the dialog keeps its Create button off. */
export type FolderChoice = {
  env: Env;
  path: string;
  isGit: boolean;
  writable: boolean | null;
  project: FolderOwner | null;
  problem: "" | "project" | "chat" | "service" | "unreadable" | "missing" | "hostdown";
  /** A container folder that is not mounted yet, accepted so the launcher mounts it on the next start. */
  unmounted?: boolean;
  /** Typed while nothing could check it: an outdated host daemon. */
  unchecked?: boolean;
};

const lastKey = (env: Env) => `daedalus.folders.last.${env}`;

/** The places of each side as last read, kept for this page's life: when the machine stops answering
 *  its recent folders and favourites are still worth offering, since a path can be typed from them. */
const lastPlaces = new Map<Env, Pick<Listing, "recent" | "favourites" | "roots">>();

function remembered(env: Env): string {
  try { return localStorage.getItem(lastKey(env)) ?? ""; } catch { return ""; }
}

function remember(env: Env, path: string): void {
  // A convenience of this browser only: where the list opens next time. Losing it costs one click.
  try { localStorage.setItem(lastKey(env), path); } catch { /* private mode */ }
}

function failureOf(error: unknown): Failure {
  if (error instanceof ApiError) {
    const detail = error.data.detail;
    if (detail && typeof detail === "object" && typeof (detail as Failure).code === "string") return detail as Failure;
    return { code: error.status === 503 ? "host_down" : "refused", message: errorText(error) };
  }
  return { code: "refused", message: errorText(error) };
}

function choiceOf(env: Env, path: string, entry: Pick<FolderEntry, "kind" | "project" | "is_git" | "writable"> & { readable?: boolean }): FolderChoice {
  const problem: FolderChoice["problem"] = entry.kind === "chat" ? "chat" : entry.kind === "service" ? "service"
    : entry.project ? "project" : entry.readable === false ? "unreadable" : "";
  return { env, path, isGit: entry.is_git, writable: entry.writable, project: entry.project, problem };
}

const ROOT_ICON: Record<string, IconName> = { home: "user", workspaces: "logo", data: "archive", volume: "folder" };

export type FolderBrowserProps = {
  environments?: ProjectEnvironments;
  choice: FolderChoice | null;
  onChoice: (choice: FolderChoice | null) => void;
  onOpenProject?: (id: string) => void;
  toast: (text: string) => void;
  /** Whether the desktop launcher's window shows the page: it can mount a container folder. */
  launcher?: boolean;
};

export function FolderBrowser({ environments, choice, onChoice, onOpenProject, toast, launcher = false }: FolderBrowserProps) {
  const docker = !!environments?.docker;
  const local: Env = environments?.local ?? "container";
  const [env, setEnv] = useState<Env>(choice?.env ?? local);
  // A choice kept from a draft opens on that folder; otherwise where this browser was last left.
  const [path, setPath] = useState(() => choice?.path ?? remembered(choice?.env ?? local));
  const [listing, setListing] = useState<Listing | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [loading, setLoading] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState("");
  const [making, setMaking] = useState<string | null>(null);
  const [chatsOpen, setChatsOpen] = useState(false);
  const [serviceOpen, setServiceOpen] = useState(false);
  const [favourites, setFavourites] = useState<Place[] | null>(null);
  const request = useRef(0);

  const load = useCallback(async (nextEnv: Env, nextPath: string) => {
    const mine = ++request.current;
    setLoading(true);
    try {
      const result = await api.get<Listing>(`/api/folders?env=${nextEnv}&path=${encodeURIComponent(nextPath)}`);
      if (mine !== request.current) return;
      setListing(result);
      setFavourites(result.favourites);
      lastPlaces.set(nextEnv, { recent: result.recent, favourites: result.favourites, roots: result.roots });
      // Every side's favourites come with every listing, so the machine's are offered even while it
      // is not answering.
      for (const side of ["container", "host"] as Env[]) {
        if (side !== nextEnv) lastPlaces.set(side, { recent: lastPlaces.get(side)?.recent ?? [], roots: lastPlaces.get(side)?.roots ?? [], favourites: result.favourites });
      }
      setFailure(null);
      remember(nextEnv, result.path);
    } catch (error) {
      if (mine !== request.current) return;
      setFailure(failureOf(error));
      setListing((previous) => previous && previous.env === nextEnv ? previous : null);
    } finally {
      if (mine === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void load(env, path); }, [env, path, load]);

  // The folder a listing is of is chosen until a row is: picking "this folder" is the common case once
  // the operator has walked into it.
  useEffect(() => {
    if (!listing || failure) return;
    if (choice && choice.env === env && listing.entries.some((entry) => entry.path === choice.path)) return;
    onChoice(choiceOf(env, listing.path, { ...listing.here, is_git: listing.is_git, writable: listing.writable }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listing]);

  useEffect(() => {
    if (!failure) return;
    if (failure.code === "host_down") onChoice(typed.trim() ? { env, path: typed.trim(), isGit: false, writable: null, project: null, problem: "hostdown" } : null);
    else if (failure.code === "missing" && env === "container" && docker && typed.trim()) onChoice({ env, path: typed.trim(), isGit: false, writable: null, project: null, problem: "missing" });
    else if (failure.code === "host_outdated" && typed.trim() && !pathProblem(typed)) onChoice({ env, path: typed.trim(), isGit: false, writable: null, project: null, problem: "", unchecked: true });
    else if (failure.code !== "host_outdated") onChoice(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failure]);

  function go(next: string, nextEnv: Env = env) {
    if (nextEnv !== env) {
      setHistory([]);
      setEnv(nextEnv);
      setPath(next);
      setListing(null);
      setFavourites(lastPlaces.get(nextEnv)?.favourites ?? null);
      return;
    }
    if (listing && next === listing.path) return void load(env, next);
    if (listing) setHistory((items) => [...items.slice(-30), listing.path]);
    setPath(next);
  }

  function back() {
    const previous = history[history.length - 1];
    if (previous === undefined) return;
    setHistory((items) => items.slice(0, -1));
    setPath(previous);
  }

  function submitTyped() {
    const value = typed.trim();
    if (!value) { setEditing(false); return; }
    if (pathProblem(value) && !value.startsWith("~")) { toast(t("fb.path.full")); return; }
    setEditing(false);
    go(value);
  }

  async function makeFolder() {
    const name = (making ?? "").trim();
    if (!listing || !name) { setMaking(null); return; }
    try {
      const made = await api.post<{ path: string }>("/api/folders/mkdir", { env, path: listing.path, name });
      setMaking(null);
      await load(env, listing.path);
      onChoice({ env, path: made.path, isGit: false, writable: true, project: null, problem: "" });
    } catch (error) { toast(failureOf(error).message); }
  }

  async function toggleFavourite(target: string) {
    const on = !(favourites ?? []).some((place) => place.env === env && place.path === target);
    try {
      const result = await api.put<{ favourites: Place[] }>("/api/folders/favourites", { env, path: target, on });
      setFavourites(result.favourites);
    } catch (error) { toast(errorText(error)); }
  }

  const rows = listing?.entries ?? [];
  const plain = rows.filter((row) => row.kind === "folder");
  const chats = rows.filter((row) => row.kind === "chat");
  const service = rows.filter((row) => row.kind === "service");
  const current = listing?.path ?? "";
  const mine = (favourites ?? []).filter((place) => !place.env || place.env === env);
  const pinned = mine.some((place) => place.path === current);
  const hostDown = env === "host" && failure?.code === "host_down";
  const showPlaces = !!listing || hostDown || failure?.code === "host_outdated" || failure?.code === "missing";
  const places = listing ?? lastPlaces.get(env) ?? null;

  function pick(entry: FolderEntry) {
    onChoice(choiceOf(env, entry.path, entry));
  }

  function rowKeys(event: KeyboardEvent<HTMLButtonElement>, entry: FolderEntry) {
    if (event.key === "Enter" || event.key === "ArrowRight") { event.preventDefault(); if (entry.readable) go(entry.path); }
    if (event.key === "Backspace" && listing?.parent) { event.preventDefault(); go(listing.parent); }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const buttons = [...(event.currentTarget.closest(".fb-list")?.querySelectorAll<HTMLButtonElement>("button.fb-row") ?? [])];
      const at = buttons.indexOf(event.currentTarget);
      buttons[Math.max(0, Math.min(buttons.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)))]?.focus();
    }
  }

  const row = (entry: FolderEntry, sub = false) => {
    const selected = choice?.env === env && choice.path === entry.path;
    const blocked = entry.kind !== "folder" || !!entry.project;
    const label = entry.kind === "chat" ? t("fb.chat.row", { title: entry.chat_title ?? entry.name }) : entry.name;
    return (
      <button key={entry.path} type="button" role="option" aria-selected={selected} data-path={entry.path}
        className={`fb-row ${selected ? "sel" : ""} ${blocked ? "dim" : ""} ${sub ? "sub" : ""}`}
        onClick={() => pick(entry)} onDoubleClick={() => entry.readable && go(entry.path)} onKeyDown={(event) => rowKeys(event, entry)}
        title={entry.path}>
        <Icon name={entry.kind === "chat" ? "bots" : entry.kind === "service" ? "logo" : "folder"} size={15} />
        <span className="fb-name">{label}{entry.kind === "chat" && <span className="fb-hash">{entry.name}</span>}</span>
        {entry.is_git && <span className="fb-badge">{t("comp.name.git")}</span>}
        {entry.kind === "service" && <span className="fb-badge">{t("fb.service.badge")}</span>}
        {entry.project && entry.kind === "folder" && <span className="fb-badge acc">{t("fb.project.badge", { name: entry.project.name })}</span>}
        {entry.writable === false && entry.kind === "folder" && <span className="fb-badge warn">{t("fb.readonly")}</span>}
        <span className="fb-time">{entry.mtime ? relTime(entry.mtime * 1000) : ""}</span>
      </button>
    );
  };

  const placeRow = (place: Place, icon: IconName, cls = "") => (
    <button key={`${cls}${place.path}`} type="button" className={`fb-place ${place.path === current ? "on" : ""} ${cls}`}
      onClick={() => go(place.path)} title={place.path}>
      <Icon name={icon} size={14} /><span className="truncate">{place.name}</span>
    </button>
  );

  return (
    <div className="fb" data-env={env}>
      <div className="fb-bar">
        {docker && (
          <div className="fb-seg" role="radiogroup" aria-label={t("fb.where")}>
            <button type="button" role="radio" aria-checked={env === "container"} className={env === "container" ? "on" : ""} onClick={() => env !== "container" && go(remembered("container"), "container")}>
              <Icon name="bots" size={13} />{t("fb.container")}
            </button>
            <button type="button" role="radio" aria-checked={env === "host"} className={env === "host" ? "on" : ""} onClick={() => env !== "host" && go(remembered("host"), "host")}>
              <Icon name="lock" size={13} />{t("fb.host")}
              {env === "host" && hostDown && <i className="fb-down" aria-label={t("fb.host.down.short")} />}
            </button>
          </div>
        )}
        <button type="button" className="iconbtn small" onClick={back} disabled={!history.length} title={t("fb.back")} aria-label={t("fb.back")}><Icon name="back" size={15} /></button>
        <button type="button" className="iconbtn small" onClick={() => listing?.parent && go(listing.parent)} disabled={!listing?.parent} title={t("fb.up")} aria-label={t("fb.up")}><Icon name="up" size={15} /></button>
        {editing ? (
          <input className="fb-crumbs edit mono" autoFocus value={typed} spellCheck={false} aria-label={t("fb.path")}
            placeholder={t("fb.path.placeholder")}
            onChange={(event) => setTyped(event.target.value)} onBlur={() => typed.trim() === current && setEditing(false)}
            onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); submitTyped(); } if (event.key === "Escape") { event.stopPropagation(); setEditing(false); } }} />
        ) : (
          <div className="fb-crumbs mono" onClick={(event) => { if (event.target === event.currentTarget) { setTyped(current); setEditing(true); } }} title={t("fb.path.edit")}>
            {(listing?.crumbs ?? []).map((crumb, index, all) => (
              <span key={crumb.path} className="fb-crumb-wrap">
                {index > 0 && <span className="fb-sep">/</span>}
                <button type="button" className={`fb-crumb ${index === all.length - 1 ? "last" : ""}`} onClick={() => go(crumb.path)}>{crumb.name}</button>
              </span>
            ))}
            <button type="button" className="fb-crumb-edit" onClick={() => { setTyped(current); setEditing(true); }} aria-label={t("fb.path.edit")} title={t("fb.path.edit")}><Icon name="pen" size={12} /></button>
          </div>
        )}
        {listing && (
          <button type="button" className={`iconbtn small fb-pin ${pinned ? "pinned" : ""}`} aria-pressed={pinned} onClick={() => void toggleFavourite(current)}
            title={t(pinned ? "fb.fav.remove" : "fb.fav.add")} aria-label={t(pinned ? "fb.fav.remove" : "fb.fav.add")}><Icon name="flag" size={14} /></button>
        )}
        <button type="button" className="btn small fb-mk" disabled={!listing || listing.writable === false} onClick={() => setMaking("")}><Icon name="plus" size={13} />{t("fb.mkdir")}</button>
      </div>
      <div className="fb-main">
        {showPlaces && (
          <nav className="fb-places" aria-label={t("fb.places")}>
            {!!places?.recent.length && <><div className="fb-places-h">{t("fb.recent")}</div>{places.recent.map((place) => placeRow(place, "clock", "recent"))}</>}
            {!!mine.length && <><div className="fb-places-h">{t("fb.favourites")}</div>{mine.map((place) => placeRow(place, "flag", "fav"))}</>}
            {!!places?.roots.length && <><div className="fb-places-h">{t("fb.roots")}</div>{places.roots.map((place) => placeRow({ ...place, name: rootName(place) }, ROOT_ICON[place.kind ?? ""] ?? "folder", "root"))}</>}
          </nav>
        )}
        <div className="fb-list" role="listbox" aria-label={t("fb.list")} aria-busy={loading}>
          {failure ? <FailurePanel failure={failure} env={env} typed={typed} onRetry={() => void load(env, path)}
            onType={() => { setTyped(typed || current); setEditing(true); }} /> : (
            <>
              {making !== null && (
                <div className="fb-row fb-making">
                  <Icon name="folder" size={15} />
                  <input className="fb-mk-input" autoFocus value={making} placeholder={t("fb.mkdir.placeholder")} aria-label={t("fb.mkdir.name")}
                    onChange={(event) => setMaking(event.target.value)} onBlur={() => !making?.trim() && setMaking(null)}
                    onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); void makeFolder(); } if (event.key === "Escape") { event.stopPropagation(); setMaking(null); } }} />
                </div>
              )}
              {plain.map((entry) => row(entry))}
              {chats.length > 0 && (
                <>
                  <button type="button" className="fb-group" aria-expanded={chatsOpen} onClick={() => setChatsOpen((open) => !open)}>
                    <span className={`chev ${chatsOpen ? "down" : ""}`}>›</span><b>{t("fb.chats")}</b><span>· {chats.length} · {t("fb.chats.why")}</span>
                  </button>
                  {chatsOpen && chats.map((entry) => row(entry, true))}
                </>
              )}
              {service.length > 0 && (
                <>
                  <button type="button" className="fb-group" aria-expanded={serviceOpen} onClick={() => setServiceOpen((open) => !open)}>
                    <span className={`chev ${serviceOpen ? "down" : ""}`}>›</span><b>{t("fb.service")}</b><span>· {service.length} · {service.map((entry) => entry.project?.name ?? entry.name).join(", ")}</span>
                  </button>
                  {serviceOpen && service.map((entry) => row(entry, true))}
                </>
              )}
              {listing && !rows.length && making === null && <div className="fb-none sub">{t("fb.empty")}</div>}
              {listing?.truncated && <div className="fb-none sub">{t("fb.truncated")}</div>}
            </>
          )}
        </div>
      </div>
      <ChoiceLine choice={choice} env={env} failure={failure} launcher={launcher} environments={environments} onOpenProject={onOpenProject}
        onAccept={(next) => onChoice(next)} onHost={() => choice && go(choice.path, "host")} />
    </div>
  );
}

function rootName(place: Place): string {
  if (place.kind === "home") return t("fb.root.home");
  if (place.kind === "workspaces") return t("fb.root.workspaces");
  if (place.kind === "data") return t("fb.root.data");
  return place.name;
}

/** Why the list is empty: the machine not answering, its daemon too old, a folder that is not there. */
function FailurePanel({ failure, env, typed, onRetry, onType }: { failure: Failure; env: Env; typed: string; onRetry: () => void; onType: () => void }) {
  if (failure.code === "host_down") {
    return (
      <div className="fb-empty" role="status">
        <Icon name="offline" size={24} />
        <b>{t("fb.host.down.title")}</b>
        <p>
          {failure.configured ? t("fb.host.down.body") : t("fb.host.missing.body")} <code>{failure.configured ? failure.start : failure.install}</code>
          {failure.configured && <> ({t("fb.host.down.install")} <code>{failure.install}</code>)</>}. {t("fb.host.down.type")}
        </p>
        <div className="fb-empty-act">
          <button type="button" className="btn" onClick={onRetry}><Icon name="reload" size={14} />{t("fb.retry")}</button>
          <button type="button" className="btn ghost" onClick={onType}><Icon name="pen" size={14} />{t("fb.type")}</button>
        </div>
      </div>
    );
  }
  if (failure.code === "host_outdated") {
    return (
      <div className="fb-empty" role="status">
        <Icon name="alert" size={24} />
        <b>{t("fb.host.old.title")}</b>
        <p>{t("fb.host.old.body")} <code>{failure.install}</code></p>
        <div className="fb-empty-act"><button type="button" className="btn ghost" onClick={onType}><Icon name="pen" size={14} />{t("fb.type")}</button></div>
      </div>
    );
  }
  if (failure.code === "missing") {
    const path = typed.trim();
    return (
      <div className="fb-empty" role="status">
        <Icon name="folder" size={24} />
        <b>{t(env === "container" && failure.mount ? "fb.missing.container" : "fb.missing")}</b>
        {path && <p><span className="mono">{path}</span> {t(failure.on_host ? "fb.missing.onhost" : failure.mount ? "fb.missing.mount" : "fb.missing.none")}</p>}
      </div>
    );
  }
  return (
    <div className="fb-empty" role="status">
      <Icon name="alert" size={24} />
      <b>{t("fb.refused")}</b>
      <p>{failure.message}</p>
      <div className="fb-empty-act"><button type="button" className="btn ghost" onClick={onType}><Icon name="pen" size={14} />{t("fb.type")}</button></div>
    </div>
  );
}

/** The line under the list: what is chosen and what was found about it, or why it cannot be chosen. */
function ChoiceLine({ choice, env, failure, launcher, environments, onOpenProject, onAccept, onHost }: { choice: FolderChoice | null; env: Env; failure: Failure | null;
  launcher: boolean; environments?: ProjectEnvironments; onOpenProject?: (id: string) => void; onAccept: (choice: FolderChoice) => void; onHost: () => void }) {
  const [composeShown, setComposeShown] = useState(false);
  const compose = useMemo(() => choice ? `- ${choice.path}:${choice.path}` : "", [choice]);
  if (!choice || choice.env !== env) {
    if (failure?.code === "host_down" || failure?.code === "missing") return null;
    return <div className="fb-foot sub">{t("fb.choose")}</div>;
  }
  if (choice.problem === "project" && choice.project) {
    return (
      <div className="fb-foot bad" role="alert">
        <Icon name="alert" size={14} />
        <span className="grow">{t("fb.taken", { name: choice.project.name })}</span>
        {onOpenProject && <button type="button" className="btn small" onClick={() => onOpenProject(choice.project!.id)}>{t("fb.taken.open")}</button>}
      </div>
    );
  }
  if (choice.problem === "chat") return <div className="fb-foot" role="status">{t("fb.chat.hint")}</div>;
  if (choice.problem === "service") return <div className="fb-foot" role="status">{t("fb.service.hint")}</div>;
  if (choice.problem === "unreadable") return <div className="fb-foot bad" role="alert"><Icon name="alert" size={14} />{t("fb.unreadable")}</div>;
  if (choice.problem === "hostdown") return <div className="fb-foot" role="status"><span className="fb-label">{t("fb.chosen")}</span><span className="mono truncate">{choice.path}</span><span className="grow" /><span className="sub">{t("fb.host.later")}</span></div>;
  if (choice.problem === "missing") {
    const canHost = !!environments?.host_bridge;
    return (
      <div className="fb-warn" role="status">
        <Icon name="alert" size={15} />
        <span className="grow">{launcher ? t("fb.mount.launcher") : composeShown ? <>{t("fb.mount.compose")} <code className="mono">{compose}</code></> : t("fb.mount.nolauncher")}</span>
        {launcher || composeShown
          ? <button type="button" className="btn small" onClick={() => onAccept({ ...choice, problem: "", unmounted: true })}>{t(launcher ? "fb.mount" : "fb.mount.anyway")}</button>
          : <button type="button" className="btn small" onClick={() => setComposeShown(true)}>{t("fb.mount.howto")}</button>}
        {canHost && <button type="button" className="btn small" onClick={onHost}>{t("fb.mount.host")}</button>}
      </div>
    );
  }
  return (
    <div className="fb-foot" role="status" data-chosen={choice.path}>
      <span className="fb-label">{t("fb.chosen")}</span>
      <span className="mono truncate">{choice.path}</span>
      <span className="grow" />
      {choice.unmounted ? <span className="sub">{t(launcher ? "fb.mount.after" : "fb.mount.after.compose")}</span>
        : choice.unchecked ? <span className="sub">{t("fb.unchecked")}</span>
        : choice.env === "host" && environments?.docker
          ? <><span className="fb-host"><Icon name="lock" size={10} />{t("fb.host.tag")}</span><span className="sub">{t("fb.host.runs")}</span></>
          : <span className={choice.writable === false ? "fb-warnline" : "fb-okline"}>
              <Icon name={choice.writable === false ? "alert" : "check"} size={13} />
              {[choice.isGit ? t("comp.name.git") : "", choice.writable === false ? t("fb.readonly") : t("fb.writable")].filter(Boolean).join(" · ")}
            </span>}
    </div>
  );
}
