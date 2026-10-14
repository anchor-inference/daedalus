// The Components page's own data: what the installation is missing, and how a frame off the install
// stream is folded into what is already on the screen.
//
// Same discipline as the two speech pickers next door, and for the same reason: a view that arrives
// half-filled must not be able to take a screen down. Every optional field has a value here, an
// answer is merged into the view rather than replacing it, and progress — which lives only in this
// browser, because it arrives on a stream and not in a view — survives a reload of the list.

import { api } from "./api";

/** Where a component stands. `installing` is this browser's word for one with a live frame on it. */
export type ComponentState = "installed" | "missing" | "installing" | "unavailable";

/** One frame of /api/components/stream. */
export type InstallProgress = {
  id: string;
  state: string;
  /** A line of output from whatever is doing the work — uv's, or the launcher's. English as written. */
  step: string;
  /** The one step that is the app's own sentence rather than somebody's output, as a key. */
  step_key: string;
  error: string;
  restart_required: boolean;
};

export type ComponentEntry = {
  id: string;
  state: ComponentState;
  /** One sentence of runtime fact from the server, in English: what was found, or why not. Shown
   *  only where the server sent no key for it — it is the fallback, not the line the page draws. */
  detail: string;
  /** The same fact as a key this app writes out in the reader's language, with the holes filled
   *  from `detail_args`. The detail line is the largest body text on a card; an English sentence
   *  in the middle of a Russian page reads as a gap rather than as a term of art. */
  detail_key: string;
  detail_args: Record<string, string>;
  installable: boolean;
  /** `extra` | `launcher` | `models` | `service` | `none` — which of the five ways this one arrives. */
  how: string;
  /** The command or the image tag that installs it where the app cannot. */
  fix: string;
  download_bytes: number;
  disk_bytes: number;
  requires_restart: boolean;
  installed_count: number;
  total_count: number;
  /** Skills held back for want of this component, by folder name. */
  skills: string[];
  /** Stable keys the page writes out in the reader's language. */
  enables: string[];
  progress?: InstallProgress | null;
  /** A component that is a running process (SearXNG): whether it runs, where, and why not. Absent
   *  for the others, and for one that is not installed. */
  service?: ServiceState | null;
  /** Whether this installation can take it away again — only what it put in place itself. */
  removable?: boolean;
};

export type ServiceState = {
  /** `starting` | `running` | `failed` | `stopped`. */
  state: string;
  url: string;
  error: string;
  retry_in: number;
};

export type ComponentsView = {
  /** `native` or `docker` — the header line, and the reason half the buttons exist or do not. */
  mode: string;
  /** Whether a launcher is holding this installation. Without one, nothing fetched by the launcher
   *  can be installed from here, however native the installation is. */
  launcher: boolean;
  components: ComponentEntry[];
  disk_bytes: number;
  missing: string[];
  busy: string;
};

export const EMPTY_COMPONENTS: ComponentsView = {
  mode: "docker",
  launcher: false,
  components: [],
  disk_bytes: 0,
  missing: [],
  busy: "",
};

/** An answer that may be missing anything, as a complete view over the one the page already had. */
export function mergeComponents(previous: ComponentsView | null, answer: Partial<ComponentsView> | null | undefined): ComponentsView {
  const base = previous ?? EMPTY_COMPONENTS;
  const next = answer ?? {};
  const list = Array.isArray(next.components) ? next.components : base.components;
  const before = new Map(base.components.map((c) => [c.id, c]));
  return {
    ...base,
    ...next,
    components: list.map((c) => (c.progress ? c : { ...c, progress: before.get(c.id)?.progress ?? null })),
    missing: next.missing ?? base.missing,
  };
}

/** Read one frame off the stream. Anything unrecognised is dropped rather than guessed at. */
export function installFrame(raw: unknown): InstallProgress | null {
  if (!raw || typeof raw !== "object") return null;
  const body = raw as Record<string, unknown>;
  if (typeof body.id !== "string" || !body.id) return null;
  return {
    id: body.id,
    state: String(body.state ?? ""),
    step: String(body.step ?? ""),
    step_key: String(body.step_key ?? ""),
    error: String(body.error ?? ""),
    restart_required: Boolean(body.restart_required),
  };
}

/** Put a frame on the component it belongs to, and nowhere else. */
export function applyFrame(view: ComponentsView, frame: InstallProgress): ComponentsView {
  return {
    ...view,
    busy: frame.state === "queued" || frame.state === "running" ? frame.id : view.busy === frame.id ? "" : view.busy,
    components: view.components.map((c) =>
      c.id === frame.id
        ? { ...c, progress: frame, state: frame.state === "queued" || frame.state === "running" ? "installing" : c.state }
        : c,
    ),
  };
}

/** Whether an install has finished, either way — the moment the list is worth reloading. */
export const settled = (frame: InstallProgress): boolean =>
  frame.state === "installed" || frame.state === "failed" || frame.state === "cancelled";

/** Whether anything installed in this visit still needs the restart it asked for. */
export function restartPending(view: ComponentsView): boolean {
  return view.components.some((c) => c.progress?.state === "installed" && c.progress.restart_required);
}

export const fetchComponents = () => api.get<Partial<ComponentsView>>("/api/components");

export const installComponent = (id: string) => api.post<InstallProgress>(`/api/components/${encodeURIComponent(id)}/install`, {});

export const cancelComponent = (id: string) => api.post<{ cancelled: boolean }>(`/api/components/${encodeURIComponent(id)}/cancel`, {});

export const removeComponent = (id: string) => api.post<ComponentEntry>(`/api/components/${encodeURIComponent(id)}/remove`, {});

export const restartAgent = () => api.post<{ result: string }>("/api/components/restart", {});
