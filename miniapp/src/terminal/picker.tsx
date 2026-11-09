// Where a new terminal runs: the one question every "+" asks, in the session's dock, on the Terminals
// page and on a phone. Asked every time on purpose — a host terminal has the whole machine and a
// container one has a sandboxed copy of the work, and the operator wants to choose that each time
// rather than find out afterwards which one a bare "+" made. The quick path is the mark on the place
// chosen last, which also takes the keyboard, so Enter repeats it.
//
// The sandbox switch lives here too, beside the places it applies to: the choice of environment and
// the choice of isolation are one decision.

import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { Project, TerminalEnv, TerminalEnvName } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { Popover } from "../ui/dialogs";
import { BottomSheet } from "../ui/phone";
import { loadSandboxChoice, sandboxOffer, sandboxToggle, saveSandboxChoice } from "./dockstate";

export type PlaceChoice = "container" | "host" | "folder";
export const CHOICES: PlaceChoice[] = ["container", "host", "folder"];

/** What the operator picked: where, in which folder (none: the environment's own default), and
 *  whether in the sandbox. */
export type PickedPlace = { choice: PlaceChoice; env: TerminalEnvName; cwd?: string; projectId?: string | null; sandbox: boolean };

/** The folder each of the two quick places starts in, as the caller knows it: shown under the
 *  place, and passed on as the folder. Null when the environment chooses (the session's own folder,
 *  the container's home). */
export type PickerFolders = { container: { path: string | null; projectId?: string | null }; host: { path: string | null } };

const LAST_KEY = "daedalus.term.lastPlace";

export function loadLastPlace(): PlaceChoice | null {
  try {
    const value = localStorage.getItem(LAST_KEY);
    return value === "container" || value === "host" || value === "folder" ? value : null;
  } catch {
    return null;
  }
}

export function saveLastPlace(choice: PlaceChoice): void {
  try {
    localStorage.setItem(LAST_KEY, choice);
  } catch {
    /* remembered for this page only */
  }
}

/** Why a place cannot be chosen now, or "" when it can. With the sandbox switched on, an environment
 *  that cannot give one is closed too: a terminal asked for in a sandbox is not opened without it. */
export function placeBlocked(choice: PlaceChoice, envs: TerminalEnv[], boxed: boolean): string {
  if (choice === "folder") return envs.some((e) => e.available && (!boxed || sandboxOffer(e).ok)) ? "" : unavailable(envs);
  const env = envs.find((e) => e.env === choice);
  if (!env?.available) return t(`term.unavailable.${choice}`);
  const offer = sandboxOffer(env);
  return boxed && !offer.ok ? t("term.sandbox.unavailable", { reason: offer.reason || t("term.unavailable.none") }) : "";
}

function unavailable(envs: TerminalEnv[]): string {
  const container = envs.find((e) => e.env === "container");
  return container && container.reason === "not_installed" && !envs.some((e) => e.available) ? t("term.unavailable.container") : t("term.unavailable.none");
}

/** The sandbox as it applies now: on only when asked for and some environment can give it. */
export function effectiveSandbox(envs: TerminalEnv[], boxed: boolean): boolean {
  return boxed && sandboxToggle(envs).ok;
}

type PickerProps = {
  envs: TerminalEnv[];
  folders: PickerFolders;
  /** The projects whose folders the "Other folder" step suggests. */
  projects?: Project[];
  /** The project in view: its folder wins a tie when two projects share one. */
  lens?: Project | null;
  /** The control the picker hangs from on a desktop; on a phone it is a sheet from the bottom. */
  anchor: HTMLElement | null;
  /** Which edge of the anchor the popover lines up with: the dock's "+" sits at the left, the page's
   *  button at the right. */
  align?: "left" | "right";
  phone: boolean;
  /** The place marked when nothing was chosen on this device yet: the project's default environment. */
  suggested?: PlaceChoice;
  onPick: (place: PickedPlace) => Promise<unknown> | unknown;
  onClose: () => void;
};

export function TerminalPicker({ envs, folders, projects = [], lens = null, anchor, align = "right", phone, suggested, onPick, onClose }: PickerProps) {
  const [boxed, setBoxed] = useState(loadSandboxChoice);
  const [last] = useState(() => loadLastPlace() ?? suggested ?? null);
  const [step, setStep] = useState<"places" | "folder">("places");
  const [busy, setBusy] = useState(false);
  const sandbox = sandboxToggle(envs);
  const sandboxed = effectiveSandbox(envs, boxed);

  const go = async (place: Omit<PickedPlace, "sandbox">) => {
    if (busy) return;
    setBusy(true);
    saveLastPlace(place.choice);
    try {
      await onPick({ ...place, sandbox: sandboxed });
    } finally {
      setBusy(false);
      onClose();
    }
  };

  const choose = (choice: PlaceChoice) => {
    if (placeBlocked(choice, envs, sandboxed) || busy) return;
    if (choice === "folder") setStep("folder");
    else if (choice === "container") void go({ choice, env: "container", cwd: folders.container.path ?? undefined, projectId: folders.container.projectId ?? null });
    else void go({ choice, env: "host", cwd: folders.host.path ?? undefined, projectId: null });
  };

  const toggle = () => {
    setBoxed((on) => {
      saveSandboxChoice(!on);
      return !on;
    });
  };

  // 1, 2 and 3 choose the place with that number; the arrows, Enter and Escape are the popover's own.
  const keys = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (step !== "places" || e.ctrlKey || e.metaKey || e.altKey) return;
    if ((e.target as HTMLElement).closest("input, textarea")) return;
    const at = ["Digit1", "Digit2", "Digit3", "Numpad1", "Numpad2", "Numpad3"].indexOf(e.code);
    if (at < 0) return;
    e.preventDefault();
    choose(CHOICES[at % 3]);
  };

  const sub: Record<PlaceChoice, string> = {
    container: folders.container.path || t("term.new.container.home"),
    host: folders.host.path || envs.find((e) => e.env === "host")?.home || t("term.new.host.home"),
    folder: t("term.pick.folder.sub"),
  };

  const body = step === "places" ? (
    <div className="term-pick" role="group" aria-label={t("term.pick.title")} onKeyDown={keys}>
      {!phone && <div className="term-pick-title">{t("term.pick.title")}</div>}
      {CHOICES.map((choice, i) => {
        const why = placeBlocked(choice, envs, sandboxed);
        return (
          <button
            key={choice}
            type="button"
            role="menuitem"
            className={`term-pick-row ${choice}`}
            data-choice={choice}
            disabled={!!why || busy}
            aria-current={choice === last && !why ? "true" : undefined}
            title={why || undefined}
            onClick={() => choose(choice)}
          >
            <Icon name={choice === "host" ? "lock" : choice === "folder" ? "folder" : "terminal"} size={16} />
            <span className="term-pick-text">
              <span className="term-pick-name">
                {t(`term.pick.${choice}`)}
                {choice === "container" && <span className="term-pick-tag ok">{t("term.pick.isolated")}</span>}
                {choice === "host" && <span className="term-pick-tag warn">{t("term.pick.full")}</span>}
                {choice === last && !why && <span className="term-pick-last">{t("term.pick.last")}</span>}
              </span>
              <span className={`term-pick-sub truncate ${why ? "why" : choice === "folder" ? "" : "mono"}`}>{why || sub[choice]}</span>
            </span>
            {!phone && <kbd className="term-pick-key">{i + 1}</kbd>}
          </button>
        );
      })}
      <div className="menu-sep" />
      <button
        type="button"
        role="menuitemcheckbox"
        className="term-pick-row sandbox"
        aria-checked={sandboxed}
        disabled={!sandbox.ok}
        title={sandbox.ok ? t("term.sandbox.hint") : t("term.sandbox.unavailable", { reason: sandbox.reason || t("term.unavailable.none") })}
        onClick={toggle}
        data-choice="sandbox"
      >
        <Icon name="shield" size={16} />
        <span className="term-pick-text">
          <span className="term-pick-name">{t("term.sandbox.toggle")}</span>
          <span className="term-pick-sub why">{sandbox.ok ? t("term.sandbox.hint") : t("term.sandbox.unavailable", { reason: sandbox.reason || t("term.unavailable.none") })}</span>
        </span>
        <span className={`menu-check ${sandboxed ? "on" : ""}`} aria-hidden="true">{sandboxed && <Icon name="check" size={14} />}</span>
      </button>
    </div>
  ) : (
    <FolderStep envs={envs} boxed={sandboxed} folders={folders} projects={projects} lens={lens} busy={busy} onBack={() => setStep("places")} onOpen={(env, cwd, projectId) => void go({ choice: "folder", env, cwd, projectId })} />
  );

  if (phone) return <BottomSheet title={t("term.pick.title")} onClose={onClose} className="term-pick-sheet">{body}</BottomSheet>;
  return (
    <Popover anchor={anchor} onClose={onClose} className="term-pick-pop" align={align} label={t("term.pick.title")}>
      {body}
    </Popover>
  );
}

/** "Other folder…": the environment, and a folder typed, picked from the projects' folders there, or
 *  browsed for on the desktop app. */
function FolderStep({ envs, boxed, folders, projects, lens, busy, onBack, onOpen }: { envs: TerminalEnv[]; boxed: boolean; folders: PickerFolders; projects: Project[]; lens: Project | null; busy: boolean; onBack: () => void; onOpen: (env: TerminalEnvName, cwd: string, projectId: string | null) => void }) {
  const usable = (name: TerminalEnvName) => !placeBlocked(name, envs, boxed);
  const [where, setWhere] = useState<TerminalEnvName>(usable("container") ? "container" : "host");
  const start = (name: TerminalEnvName) => (name === "container" ? folders.container.path ?? "" : folders.host.path ?? envs.find((e) => e.env === "host")?.home ?? "");
  const [path, setPath] = useState(() => start(where));
  const input = useRef<HTMLInputElement>(null);
  const pick = window.daedalus?.pickFolder;
  useEffect(() => input.current?.focus(), []);
  // Suggestions are the projects' folders in the chosen environment: a container path means nothing
  // on the host and the other way round. A folder of several projects goes to the one in view.
  const suggestions = projects.flatMap((p) => p.folders.filter((f) => f.env === where).map((f) => ({ path: f.path, project: p })));
  const matching = suggestions.filter((s) => s.path === path.trim());
  const projectId = (matching.find((s) => s.project.id === lens?.id) ?? matching[0])?.project.id ?? (where === "container" && path.trim() === folders.container.path ? folders.container.projectId ?? null : null);
  const choose = (name: TerminalEnvName) => {
    setWhere(name);
    setPath(start(name));
  };
  return (
    <form className="term-pick-folder" onSubmit={(e) => { e.preventDefault(); if (path.trim() && usable(where)) onOpen(where, path.trim(), projectId); }}>
      <div className="segmented inline" role="radiogroup" aria-label={t("term.new.where")}>
        {(["container", "host"] as TerminalEnvName[]).map((name) => (
          <button key={name} type="button" role="radio" aria-checked={where === name} className={where === name ? "on" : ""} disabled={!usable(name)} title={placeBlocked(name, envs, boxed) || undefined} onClick={() => choose(name)}>
            {name === "host" && <Icon name="lock" size={12} />}
            {t(`term.env.${name}`)}
          </button>
        ))}
      </div>
      <div className="term-new-path">
        <input ref={input} className="field" value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("term.new.path")} aria-label={t("term.new.path")} list="term-pick-suggestions" autoCapitalize="off" spellCheck={false} />
        {pick && <button type="button" className="btn small" onClick={() => void pick().then((picked) => picked && setPath(picked))}>{t("term.new.browse")}</button>}
      </div>
      <datalist id="term-pick-suggestions">
        {suggestions.map((s) => <option key={`${s.project.id}:${s.path}`} value={s.path}>{s.project.name}</option>)}
      </datalist>
      {suggestions.length > 0 && (
        <div className="term-new-suggest">
          {suggestions.slice(0, 6).map((s) => (
            <button key={`${s.project.id}:${s.path}`} type="button" className="chip select" aria-pressed={path === s.path} onClick={() => setPath(s.path)} title={s.path}>
              {/* A project with two folders here names the folder too, or its two chips read the same. */}
              {suggestions.filter((o) => o.project.id === s.project.id).length > 1 ? `${s.project.name} · ${s.path.split("/").filter(Boolean).pop() ?? s.path}` : s.project.name}
            </button>
          ))}
        </div>
      )}
      <div className="term-new-actions">
        <button type="button" className="btn ghost small" onClick={onBack}>{t("shell.back")}</button>
        <button type="submit" className="btn primary small" disabled={!path.trim() || busy || !usable(where)}>{t("term.new.open")}</button>
      </div>
    </form>
  );
}
