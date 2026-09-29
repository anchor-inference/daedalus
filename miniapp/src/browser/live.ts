// One live view as the components read it: the connection, the picture, and what the socket has said
// about the group — who drives, the tabs, who else watches, the agent's last action, an open dialog.
//
// The picture never goes through React. A frame is decoded and drawn onto the viewer's canvas here, up
// to fifteen times a second, and React hears only about what changes the layout: the frame's size and
// the page's viewport (which the cursor is placed by), not its scroll or its pixels. So a page scrolling
// under the agent repaints one canvas, not the panel.

import { BrowserConnection, type ViewDeps, type ViewState } from "./connection";
import type { ActionEvent, CopiedEvent, FrameMeta, InputMessage, Tier, ViewChange, ViewControl, ViewEvent, ViewTab } from "./protocol";

export type LiveSnapshot = {
  state: ViewState;
  clientId: string | null;
  readOnly: boolean;
  control: ViewControl | null;
  tabs: ViewTab[];
  /** The agent's active tab, as the daemon last listed it. */
  active: string | null;
  /** The tab this view shows. */
  viewing: string | null;
  viewers: { count: number; others: { id: string; kind: string; label: string }[] };
  /** The latest agent action, which the cursor follows; `done` once the daemon has finished it. */
  action: (ActionEvent & { done?: boolean; ok?: boolean }) | null;
  /** Recent actions from the socket, newest first, for the log to show before it is read again. */
  recent: ActionEvent[];
  dialog: { type: string; message: string } | null;
  needs: { reason: string; what: string; url: string } | null;
  /** What the picture's geometry depends on; null before the first frame. */
  meta: FrameMeta | null;
  /** Whether a frame has been drawn at all (for the placeholder). */
  painted: boolean;
};

type Listener = () => void;

const RECENT_MAX = 50;

/** How long a copy waits for the page's answer; a page busy that long is not one to wait on. */
export const COPY_TIMEOUT_MS = 10_000;

/** What a copy brought back: the selected text, or why there is none. */
export type Copied = { text: string; truncated: boolean; withheld: boolean };

export class CopyError extends Error {}

/** Only what moves the cursor's placement: a new size or zoom, not a new scroll position. */
function sameGeometry(a: FrameMeta | null, b: FrameMeta): boolean {
  return !!a && a.w === b.w && a.h === b.h && a.vw === b.vw && a.vh === b.vh && a.page_scale === b.page_scale && a.offset_top === b.offset_top && a.tab === b.tab;
}

export type LiveOptions = {
  group: string;
  tier: Tier;
  tab?: string;
  readOnly?: boolean;
  box: () => { max_w: number; max_h: number; dpr?: number; quality?: number };
  deps?: ViewDeps;
};

export class LiveView {
  private listeners = new Set<Listener>();
  private snap: LiveSnapshot;
  private canvas: HTMLCanvasElement | null = null;
  private lastBitmap: ImageBitmap | HTMLImageElement | null = null;
  readonly connection: BrowserConnection;
  /** Frames drawn, for the checks: written on the canvas as `data-frames`, never into React. */
  frames = 0;
  /** The newest frame's metadata, scroll included. */
  latest: FrameMeta | null = null;
  private copies = new Map<string, { resolve: (c: Copied) => void; reject: (e: Error) => void; timer: number }>();
  private copySeq = 0;

  constructor(readonly options: LiveOptions) {
    this.snap = {
      state: { kind: "connecting" }, clientId: null, readOnly: !!options.readOnly, control: null, tabs: [], active: null, viewing: options.tab ?? null,
      viewers: { count: 0, others: [] }, action: null, recent: [], dialog: null, needs: null, meta: null, painted: false,
    };
    this.connection = new BrowserConnection(
      {
        group: options.group,
        tier: options.tier,
        tab: options.tab,
        readOnly: options.readOnly,
        box: options.box,
        sink: { draw: (frame) => this.draw(frame.meta, frame.image) },
        onState: (state) => this.set({ state }),
        onEvent: (event) => this.onEvent(event),
      },
      options.deps,
    );
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };

  get = (): LiveSnapshot => this.snap;

  private set(patch: Partial<LiveSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const l of this.listeners) l();
  }

  /** The canvas the picture goes on. The last picture is drawn at once, so a remount is never blank. */
  attach(canvas: HTMLCanvasElement | null): void {
    this.canvas = canvas;
    if (canvas && this.lastBitmap) this.paint(this.lastBitmap);
  }

  input(message: InputMessage): boolean {
    return this.connection.input(message);
  }

  /**
   * The page's selected text, read by the daemon in this view's input order: a key sent after the copy
   * (the cut's) acts on the page only once the selection has been read.
   */
  copy(): Promise<Copied> {
    const id = `k${++this.copySeq}`;
    return new Promise<Copied>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.copies.delete(id);
        reject(new CopyError("timeout"));
      }, COPY_TIMEOUT_MS);
      this.copies.set(id, { resolve, reject, timer });
      if (!this.input({ t: "copy", id })) {
        window.clearTimeout(timer);
        this.copies.delete(id);
        reject(new CopyError("offline"));
      }
    });
  }

  private copied(event: CopiedEvent): void {
    const waiting = this.copies.get(event.id);
    if (!waiting) return;
    this.copies.delete(event.id);
    window.clearTimeout(waiting.timer);
    if (event.error) waiting.reject(new CopyError(event.error));
    else waiting.resolve({ text: event.text ?? "", truncated: !!event.truncated, withheld: !!event.withheld });
  }

  view(change: ViewChange): void {
    if (change.tab) this.set({ viewing: change.tab });
    this.connection.view(change);
  }

  /** Point the cursor at an older action, from the log. */
  focus(action: ActionEvent): void {
    this.set({ action: { ...action, done: true } });
  }

  close(): void {
    this.connection.close();
    this.listeners.clear();
    for (const [, waiting] of this.copies) {
      window.clearTimeout(waiting.timer);
      waiting.reject(new CopyError("closed"));
    }
    this.copies.clear();
    const b = this.lastBitmap;
    if (b && "close" in b) b.close();
    this.lastBitmap = null;
  }

  private onEvent(event: ViewEvent): void {
    switch (event.type) {
      case "hello":
        return this.set({ clientId: event.client_id, readOnly: event.read_only, control: event.control, viewing: this.snap.viewing ?? event.tab_id });
      case "tabs":
        return this.set({ tabs: event.tabs, active: event.active, viewing: this.snap.viewing && event.tabs.some((t) => t.id === this.snap.viewing) ? this.snap.viewing : event.active });
      case "tab": {
        const { type: _type, ...tab } = event;
        const tabs = this.snap.tabs.some((t) => t.id === tab.id) ? this.snap.tabs.map((t) => (t.id === tab.id ? { ...t, ...tab } : t)) : [...this.snap.tabs, tab];
        return this.set({ tabs });
      }
      case "viewers":
        return this.set({ viewers: { count: event.count, others: event.others } });
      case "action":
        return this.set({ action: event, recent: [event, ...this.snap.recent.filter((a) => a.id !== event.id)].slice(0, RECENT_MAX) });
      case "action_done":
        if (this.snap.action?.id === event.id) this.set({ action: { ...this.snap.action, done: true, ok: event.ok } });
        return;
      case "control": {
        const { type: _t, group: _g, ...control } = event;
        // A person taking the wheel, or the agent getting it back, answers whatever it asked for; a
        // pause is how a request starts, so it leaves the request standing.
        return this.set({ control, ...(control.owner !== "paused" ? { needs: null } : {}) });
      }
      case "dialog": {
        const closed = event.state === "closed";
        const message = typeof event.message === "string" ? event.message : "";
        const kind = typeof event.dialog_type === "string" ? event.dialog_type : typeof (event as { kind?: unknown }).kind === "string" ? String((event as { kind?: unknown }).kind) : "alert";
        return this.set({ dialog: closed ? null : { type: kind, message } });
      }
      case "needs_you":
        return this.set({ needs: { reason: event.reason, what: event.what, url: event.url } });
      case "copied":
        return this.copied(event);
      default:
        return;
    }
  }

  private async draw(meta: FrameMeta, image: Uint8Array): Promise<void> {
    const bitmap = await decode(image);
    if (!bitmap) return;
    const old = this.lastBitmap;
    this.lastBitmap = bitmap;
    if (old && old !== bitmap && "close" in old) old.close();
    this.paint(bitmap);
    this.frames++;
    if (this.canvas) this.canvas.dataset.frames = String(this.frames);
    this.latest = meta;
    // The snapshot changes only with the geometry: React is told nothing about a scroll, and a
    // snapshot replaced without telling it would read as a store that changes under its render.
    if (!sameGeometry(this.snap.meta, meta) || !this.snap.painted) this.set({ meta, painted: true });
  }

  private paint(bitmap: ImageBitmap | HTMLImageElement): void {
    const canvas = this.canvas;
    if (!canvas) return;
    const w = "naturalWidth" in bitmap ? bitmap.naturalWidth : bitmap.width;
    const h = "naturalHeight" in bitmap ? bitmap.naturalHeight : bitmap.height;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  }
}

/** A JPEG as something a canvas draws; `createImageBitmap` decodes off the main thread where it can. */
async function decode(image: Uint8Array): Promise<ImageBitmap | HTMLImageElement | null> {
  const blob = new Blob([image as BlobPart], { type: "image/jpeg" });
  try {
    if (typeof createImageBitmap === "function") return await createImageBitmap(blob);
  } catch {
    /* an engine that cannot decode here falls back to an <img> */
  }
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } catch {
    return null;
  } finally {
    URL.revokeObjectURL(url);
  }
}
