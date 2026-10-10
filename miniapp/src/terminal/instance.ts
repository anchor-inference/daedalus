// One open terminal in the page: its xterm.js instance, its connection, and everything that hangs off
// them — sizing, keys, copy and paste, links, the renderer, the state a view draws its strips from.
//
// It outlives the components that show it. The registry (`registry.ts`) keeps it while it moves
// between the session dock, the full-screen view and the Terminals screen; a view only lends it a
// place in the document (`mount`) and says whether it is on screen. So nothing here may depend on a
// particular view, and nothing a view does may reset the terminal: a move re-parents one element.
//
// xterm.js itself arrives with the lazy kit (`load.ts`). Until then the instance is an empty element
// and a pending state; this file imports xterm.js for its types only, which the bundle guard checks.

import type { ILink, IDisposable, Terminal } from "@xterm/xterm";
import type { FitAddon } from "@xterm/addon-fit";
import type { SearchAddon } from "@xterm/addon-search";
import type { WebglAddon } from "@xterm/addon-webgl";
import { api, TerminalEnv } from "../api";
import { ConnectionState, guarded, TerminalConnection, TerminalSink, xtermSink } from "./connection";
import { FitContext, ResizeScheduler, Size } from "./fit";
import { flushOnlyUnparsed } from "./flush";
import { keepScrolledHistory } from "./history";
import { isMac, reservedKey, TerminalAction } from "./keys";
import { findFileLinks, resolveFileLink, rewriteLoopbackUrl } from "./links";
import { Kit, loadTerminalKit } from "./load";
import { CommandMarks, MarksSummary } from "./marks";
import type { EventMessage, KeyboardOwner, SizeOwner } from "./protocol";
import { selectableText } from "./phonekeys";
import { attachTheme, documentTokens, terminalTheme } from "./theme";
import { copyText } from "./clipboard";

/** Lines of history each terminal keeps, and asks a snapshot for. */
export const SCROLLBACK = 10_000;
/** How long after a click, a key or a layout action the person counts as using this terminal. */
export const INTERACTION_MS = 1500;

export type TerminalState = {
  /** Whether xterm.js has arrived: `failed` is a chunk that did not load (a deploy replaced it). */
  kit: "loading" | "ready" | "failed";
  connection: ConnectionState;
  title: string;
  cwd: string;
  /** The PTY's size and who set it; null until the daemon has said. */
  size: { cols: number; rows: number; owner: SizeOwner } | null;
  /** Another screen owns the size and this one would fit a different grid: the "Fit here" strip. */
  sizedElsewhere: boolean;
  keyboard: { owner: KeyboardOwner; until: number | null };
  /** Who is typing into it on the operator's behalf right now, or null. */
  agentTyping: string | null;
  altScreen: boolean;
  /** Output arrived while nobody could see it. Cleared when it is shown. */
  unseen: boolean;
  bell: boolean;
  exit: { code: number | null; signal: string | null } | null;
  progress: { state: number; value: number } | null;
  /** The shell's commands as its marks report them: the last one's result, and prompts to jump to. */
  commands: MarksSummary;
};

/** What "copy last command output" came to. */
export type CopyOutcome = "copied" | "none" | "failed";

/** After a reattach that brought no snapshot (and so no marks), how long to wait before asking the host what was missed. */
const REFILL_MS = 600;

/** What the view is asked to do on the terminal's behalf: things that need a dialog or a bar. */
export type TerminalRequest =
  | { kind: "search" }
  | { kind: "paste"; text: string; lines: number }
  | { kind: "link"; uri: string }
  /** A copy the operator asked for by key or menu: whether it reached the clipboard. */
  | { kind: "copied"; ok: boolean }
  /** The program copied (OSC 52) while the browser would not let the page write the clipboard. */
  | { kind: "copy-held" };

/** What the current view lends the terminal: where files open, the workspace they resolve against. */
export type TerminalBinding = {
  fileOpener?: (path: string, line?: number) => void;
  workspace?: string;
  /** The context the view is in now; the instance asks it whenever it wants to propose a size. */
  context?: () => Omit<FitContext, "interacted">;
};

/** Shared by every instance on the page: the environments (for links) and the font size. */
export interface InstanceShared {
  env(name: string): TerminalEnv | undefined;
  fontSize(): number;
  /** Whether this device can hold WebGL2 at all; asked once per page. */
  webgl2(): boolean;
}

const PASTE_KEY = (id: string) => `daedalus.term.paste.${id}`;
/** How long a program's refused copy waits for the copy key before it is forgotten. */
const HELD_COPY_MS = 120_000;

function remembered(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

/** The text of a buffer line and, for every UTF-16 unit of it, the cell it sits in (wide glyphs take two). */
function lineCells(line: { length: number; getCell(x: number): { getChars(): string; getWidth(): number } | undefined }): { text: string; cellOf: number[] } {
  let text = "";
  const cellOf: number[] = [];
  for (let x = 0; x < line.length; x++) {
    const cell = line.getCell(x);
    if (!cell || cell.getWidth() === 0) continue;
    const chars = cell.getChars() || " ";
    for (let i = 0; i < chars.length; i++) cellOf.push(x);
    text += chars;
  }
  return { text, cellOf };
}

export class TerminalInstance {
  /** The element the terminal lives in; views re-parent it and never recreate it. */
  readonly host: HTMLDivElement;
  private kit: Kit | null = null;
  private term: Terminal | null = null;
  private fitAddon: FitAddon | null = null;
  private searchAddon: SearchAddon | null = null;
  private webgl: WebglAddon | null = null;
  private webglBroken = false;
  private wantWebgl = false;
  private opened = false;
  private connection: TerminalConnection | null = null;
  private asleep = false;
  private resumeSeq: number | null = null;
  private scheduler: ResizeScheduler;
  private lastInteraction = 0;
  private visible = false;
  private binding: TerminalBinding = {};
  private disposables: IDisposable[] = [];
  private listeners = new Set<() => void>();
  private requestListeners = new Set<(request: TerminalRequest) => void>();
  private desired: Size | null = null;
  /** A size went out on the current socket (possibly before its `hello` arrived). */
  private sizedThisSocket = false;
  private focusOnOpen = false;
  private disposed = false;
  private stateValue: TerminalState = {
    kit: "loading",
    connection: { kind: "connecting" },
    title: "",
    cwd: "",
    size: null,
    sizedElsewhere: false,
    keyboard: { owner: "auto", until: null },
    agentTyping: null,
    altScreen: false,
    unseen: false,
    bell: false,
    exit: null,
    progress: null,
    commands: { last: null, ended: false, prompts: 0, active: false },
  };
  private marks: CommandMarks | null = null;
  private refillTimer: ReturnType<typeof setTimeout> | null = null;
  /** What the phone layer does to typed input before it leaves: the doubled-input filter and the
   *  armed modifiers. Null on a desktop, where the keyboard is a keyboard. */
  private inputHook: ((data: string) => string | null) | null = null;
  /** A key of the phone's row is on its way through xterm.js: it is already what it should be. */
  private keyInFlight = false;
  /** The program's last copy (OSC 52) that could not reach the clipboard: the next copy key delivers it. */
  private held: { text: string; at: number } | null = null;

  constructor(readonly id: string, private readonly shared: InstanceShared, private readonly readOnly = false) {
    this.host = document.createElement("div");
    this.host.className = "term-host";
    this.host.dataset.terminal = id;
    this.scheduler = new ResizeScheduler((size) => this.sendSize(size));
    // Pasting goes through here before xterm.js sees it: a capture listener on an ancestor of its
    // text field runs first, so a multi-line paste into a shell without bracketed paste can be
    // stopped and confirmed — each line of it would otherwise run as a command.
    this.host.addEventListener("paste", (e) => this.onPaste(e), true);
    this.host.addEventListener("pointerdown", () => this.interact(), true);
    this.host.addEventListener("mousedown", (e) => this.shiftSelectsOnMac(e), true);
    this.host.addEventListener("keydown", () => this.interact(), true);
    loadTerminalKit().then(
      (kit) => this.start(kit),
      () => this.patch({ kit: "failed" }),
    );
  }

  // ── what views read ──────────────────────────────────────────────────────────────────────

  get state(): TerminalState {
    return this.stateValue;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onRequest(listener: (request: TerminalRequest) => void): () => void {
    this.requestListeners.add(listener);
    return () => this.requestListeners.delete(listener);
  }

  get search(): SearchAddon | null {
    return this.searchAddon;
  }

  /** The xterm.js terminal once it is open (the browser checks read its buffer through the debug hook). */
  get terminal(): Terminal | null {
    return this.term;
  }

  /** The shell's command marks, once xterm.js has loaded (the checks read them through the debug hook). */
  get commandMarks(): CommandMarks | null {
    return this.marks;
  }

  get rendererKind(): "webgl" | "dom" {
    return this.webgl ? "webgl" : "dom";
  }

  // ── what views do ────────────────────────────────────────────────────────────────────────

  /** Put the terminal into a view's container. The first time it is in the document, it opens there. */
  mount(container: HTMLElement, binding: TerminalBinding): void {
    this.binding = binding;
    if (this.host.parentElement !== container) container.appendChild(this.host);
    this.open();
  }

  /** The view let go; the element stays wherever it is until the next view takes it. */
  unbind(binding: TerminalBinding): void {
    if (this.binding === binding) this.binding = {};
  }

  setVisible(visible: boolean): void {
    if (this.visible === visible) return;
    this.visible = visible;
    if (!visible) this.scheduler.cancel();
    else if (this.stateValue.unseen || this.stateValue.bell) this.patch({ unseen: false, bell: false });
  }

  /** The person just did something with this terminal (clicked its tab, opened it, dragged the dock). */
  interact(): void {
    this.lastInteraction = Date.now();
  }

  focus(): void {
    this.interact();
    // A terminal made a moment ago is still waiting for xterm.js to load; it takes the focus when it opens.
    if (this.term && this.opened) this.term.focus();
    else this.focusOnOpen = true;
  }

  /**
   * Measure the container and propose the size it holds. Whether the size is sent at all is `fit.ts`'s
   * rule: only a visible terminal, in a focused window or just used, ever tells the PTY its size.
   */
  fit(): void {
    if (!this.term || !this.fitAddon || !this.opened || this.disposed) return;
    // A finished process has no PTY to size; its exit banner taking a row must not send anything.
    if (this.stateValue.exit) return;
    const context = this.binding.context?.() ?? { visible: false, focused: false };
    const interacted = Date.now() - this.lastInteraction < INTERACTION_MS;
    if (!context.visible) {
      this.scheduler.cancel();
      return;
    }
    const proposed = this.fitAddon.proposeDimensions();
    if (proposed && Number.isFinite(proposed.cols) && Number.isFinite(proposed.rows)) this.desired = { cols: proposed.cols, rows: proposed.rows };
    this.scheduler.propose(proposed, { ...context, interacted });
    this.updateSizedElsewhere();
  }

  /** "Fit here": take the size for this screen now, whoever had it. */
  claimSize(): void {
    this.interact();
    this.scheduler.forget();
    this.fit();
  }

  /** Open the find bar of whichever view shows this terminal. */
  openSearch(): void {
    this.request({ kind: "search" });
  }

  paste(text: string): void {
    if (!this.term || this.readOnly) return;
    this.term.paste(text);
  }

  /**
   * Let a view filter what is typed before it is sent (the phone's doubled-input filter and sticky
   * modifiers). Returns the way to take it off again; only the hook that is on can take itself off,
   * so a view that goes after another has come cannot remove the newer one's.
   */
  setInputHook(hook: (data: string) => string | null): () => void {
    this.inputHook = hook;
    return () => {
      if (this.inputHook === hook) this.inputHook = null;
    };
  }

  /**
   * Send bytes as if typed: a key of the phone's row, or its compose line. They go through xterm.js
   * as user input, so the view scrolls to the bottom and the selection clears as it would for a key,
   * but past the input hook — they are already exactly what should be sent.
   */
  sendKeys(data: string): void {
    if (this.readOnly || !data) return;
    this.interact();
    if (!this.term) {
      this.connection?.input(data);
      return;
    }
    this.keyInFlight = true;
    try {
      this.term.input(data, true);
    } finally {
      this.keyInFlight = false;
    }
  }

  /** The two modes the phone's keys follow: which arrows to send, and whether text goes as a paste. */
  get modes(): { appCursor: boolean; bracketedPaste: boolean } {
    const modes = this.term?.modes;
    return { appCursor: !!modes?.applicationCursorKeysMode, bracketedPaste: !!modes?.bracketedPasteMode };
  }

  /** The screen's text and `history` rows above it, wrapped lines joined: the phone's selection layer. */
  screenText(history = 200): string {
    const term = this.term;
    if (!term) return "";
    return selectableText(term.buffer.active, term.rows, history);
  }

  /** Whether there is anything for Copy to take: a selection, or a copy the program asked for. */
  hasCopyable(): boolean {
    return !!this.term?.hasSelection() || this.heldCopy() !== null;
  }

  /**
   * Copies the selection, or else the program's held copy; false when there is neither. The
   * clipboard is written before the first await, so it still counts as the key press or click
   * that asked: on a plain-http page nothing else may write it.
   */
  async copySelection(): Promise<boolean> {
    const selected = this.term?.getSelection() ?? "";
    const text = selected || this.heldCopy();
    if (!text) return false;
    const ok = await copyText(text);
    if (ok && !selected) this.held = null;
    this.request({ kind: "copied", ok });
    return ok;
  }

  /** Copies the program's held copy (the toast's button, whose own tap may write the clipboard); false when nothing is held. */
  async copyHeld(): Promise<boolean> {
    const text = this.heldCopy();
    if (!text) return false;
    const ok = await copyText(text);
    if (ok) this.held = null;
    this.request({ kind: "copied", ok });
    return ok;
  }

  /** Scroll to the previous or next command's prompt; false when the shell marks none that way. */
  jumpToCommand(direction: -1 | 1): boolean {
    this.interact();
    return !!this.marks?.jump(direction);
  }

  /**
   * Copies what the last finished command printed. The buffer has it while the rows are still in its
   * history; after that the host is asked, which keeps the text of rows the browser has let go.
   */
  async copyLastOutput(): Promise<CopyOutcome> {
    const local = this.marks?.lastOutput();
    if (local === null && !this.stateValue.commands.active) return "none";
    if (local) return (await copyText(local.text)) ? "copied" : "failed";
    let text: string | null = null;
    try {
      const { commands } = await api.terminalCommands(this.id, 5, true);
      const ended = commands.filter((c) => c.end_row !== null && typeof c.output === "string");
      text = ended.length ? ended[ended.length - 1].output ?? "" : null;
    } catch {
      return "failed";
    }
    if (text === null) return "none";
    return (await copyText(text)) ? "copied" : "failed";
  }

  applyFontSize(): void {
    if (!this.term) return;
    const size = this.shared.fontSize();
    if (this.term.options.fontSize === size) return;
    this.term.options.fontSize = size;
    this.interact();
    this.fit();
  }

  applyTheme(): void {
    if (!this.term) return;
    // The page's style attribute also carries the visible height, which changes on every frame of a
    // phone's keyboard sliding in; a theme set again, even an equal one, repaints the whole screen.
    const theme = terminalTheme(documentTokens());
    const signature = JSON.stringify(theme);
    if (signature === this.themeSignature) return;
    this.themeSignature = signature;
    this.term.options.theme = theme;
  }
  private themeSignature = "";

  /** Give the terminal a WebGL renderer or take it away; the DOM renderer draws when it has none. */
  setWebgl(on: boolean): void {
    this.wantWebgl = on;
    this.applyWebgl();
  }

  /** Close the connection and keep the screen; `wake` asks for what was missed after it. */
  sleep(): void {
    if (this.asleep) return;
    this.asleep = true;
    this.resumeSeq = this.connection ? this.connection.seq : this.resumeSeq;
    this.connection?.close();
    this.connection = null;
  }

  wake(): void {
    if (!this.asleep) return;
    this.asleep = false;
    if (this.term) this.connect();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.scheduler.cancel();
    this.connection?.close();
    this.connection = null;
    if (this.refillTimer) clearTimeout(this.refillTimer);
    this.marks?.dispose();
    this.disposables.forEach((d) => d.dispose());
    this.webgl?.dispose();
    this.term?.dispose();
    this.host.remove();
    this.listeners.clear();
    this.requestListeners.clear();
  }

  // ── inside ───────────────────────────────────────────────────────────────────────────────

  private patch(next: Partial<TerminalState>): void {
    let changed = false;
    for (const key of Object.keys(next) as (keyof TerminalState)[]) {
      if (this.stateValue[key] !== next[key]) changed = true;
    }
    if (!changed) return;
    this.stateValue = { ...this.stateValue, ...next };
    this.listeners.forEach((l) => l());
  }

  private request(request: TerminalRequest): void {
    this.requestListeners.forEach((l) => l(request));
  }

  private start(kit: Kit): void {
    if (this.disposed) return;
    this.kit = kit;
    const term = new kit.Terminal({
      allowProposedApi: true,
      scrollback: SCROLLBACK,
      // Never: a program that moves the cursor down with a bare line feed (tmux, curses) would have
      // everything after it land in column 0.
      convertEol: false,
      fontFamily: kit.fontFamily,
      fontSize: this.shared.fontSize(),
      macOptionClickForcesSelection: true,
      rescaleOverlappingGlyphs: true,
      scrollOnEraseInDisplay: true,
      // Every query is answered by the daemon; these stay off so xterm.js has nothing of its own to say.
      vtExtensions: { kittyKeyboard: true, colorSchemeQuery: false },
      windowOptions: {},
      minimumContrastRatio: 4.5,
      cursorBlink: false,
      disableStdin: this.readOnly,
      theme: terminalTheme(documentTokens()),
      linkHandler: { activate: (_event, uri) => this.openUri(uri), allowNonHttpProtocols: true },
    });
    this.term = term;
    this.marks = new CommandMarks(term, () => this.patch({ commands: this.marks!.summary }));
    term.loadAddon(new kit.GhosttyUnicodeAddon());
    this.disposables.push(kit.swallowQueries(term.parser));
    this.disposables.push(keepScrolledHistory(term));
    // Before any resize: a window resized during a flood would otherwise parse output twice.
    this.disposables.push(flushOnlyUnparsed(term));
    this.fitAddon = new kit.FitAddon();
    term.loadAddon(this.fitAddon);
    this.searchAddon = new kit.SearchAddon();
    term.loadAddon(this.searchAddon);
    term.loadAddon(new kit.WebLinksAddon((_event, uri) => this.openUrl(uri)));
    // OSC 52 may put text on the clipboard; it may never read it back — a program asking for the
    // clipboard's contents would get whatever the operator last copied, passwords included.
    term.loadAddon(
      new kit.ClipboardAddon(undefined, {
        readText: () => "",
        // Claude Code copies its own mouse selection this way the moment the button is released,
        // and again on its copy keys. Outside a secure context, or in Safari after the click's
        // moment has passed, the browser refuses: the text is held for the next copy key, which
        // is a key press and so may write the clipboard.
        writeText: (_selection, text) => {
          void copyText(text).then((ok) => {
            this.held = ok ? null : { text, at: Date.now() };
            if (!ok) this.request({ kind: "copy-held" });
          });
        },
      }),
    );
    const progress = new kit.ProgressAddon();
    term.loadAddon(progress);
    this.disposables.push(progress.onChange((p) => this.patch({ progress: p.state === 0 ? null : { state: p.state, value: p.value } })));
    this.disposables.push(term.registerLinkProvider({ provideLinks: (y, callback) => callback(this.fileLinks(y)) }));
    term.attachCustomKeyEventHandler((e) => this.onKey(e));
    this.disposables.push(
      term.onData((data) => {
        const sent = this.inputHook && !this.keyInFlight ? this.inputHook(data) : data;
        if (sent) this.connection?.input(sent);
      }),
    );
    this.disposables.push(term.onBinary((data) => this.connection?.input(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff))));
    this.disposables.push(term.onTitleChange((title) => this.patch({ title })));
    this.disposables.push(term.onBell(() => this.patch({ bell: !this.visible || this.stateValue.bell })));
    this.patch({ kit: "ready" });
    this.open();
    if (!this.asleep) this.connect();
  }

  private open(): void {
    if (!this.term || this.opened || !this.host.isConnected) return;
    this.term.open(this.host);
    this.opened = true;
    this.applyWebgl();
    this.fit();
    if (this.focusOnOpen) {
      this.focusOnOpen = false;
      this.term.focus();
    }
  }

  private applyWebgl(): void {
    if (!this.term || !this.kit || !this.opened) return;
    if (this.wantWebgl && !this.webgl && !this.webglBroken && this.shared.webgl2()) {
      try {
        const addon = new this.kit.WebglAddon({ customGlyphs: true });
        addon.onContextLoss(() => {
          // The browser took the context back (too many on the page, a GPU reset). The DOM renderer
          // draws from here on; a blank terminal is the alternative.
          this.webglBroken = true;
          addon.dispose();
          if (this.webgl === addon) this.webgl = null;
        });
        this.term.loadAddon(addon);
        this.webgl = addon;
      } catch {
        this.webglBroken = true;
        this.webgl = null;
      }
    } else if (!this.wantWebgl && this.webgl) {
      this.webgl.dispose();
      this.webgl = null;
    }
  }

  private connect(): void {
    if (!this.term || this.disposed) return;
    const term = this.term;
    const base = xtermSink(term);
    const sink: TerminalSink = {
      write: (data, parsed) => {
        base.write(data, () => {
          // The marks are an extra; nothing they do may keep the stream from being acknowledged.
          if (data.length) guarded(() => this.marks?.parsed())();
          parsed();
        });
        if (!this.visible && !this.stateValue.unseen && data.length) this.patch({ unseen: true });
      },
      reset: (cols, rows) => {
        base.reset(cols, rows);
        guarded(() => this.marks?.reset())();
        // A snapshot is at the PTY's size, which is what the terminal now shows; whether this screen
        // wants another one is decided again from scratch — unless this socket already carried its
        // size, which the daemon applies after the snapshot and confirms with a `size` event.
        if (!this.sizedThisSocket) this.scheduler.forget();
      },
    };
    this.connection = new TerminalConnection(sink, {
      id: this.id,
      readOnly: this.readOnly,
      scrollback: SCROLLBACK,
      resumeSeq: this.resumeSeq,
      theme: () => attachTheme(term.options.theme ?? {}),
      onState: (state) => this.onConnection(state),
      onEvent: (event) => this.onEvent(event),
    });
  }

  private onConnection(state: ConnectionState): void {
    if (state.kind === "connecting" || state.kind === "reconnecting") this.sizedThisSocket = false;
    const exit = state.kind === "exited" ? { code: state.code, signal: state.signal } : this.stateValue.exit;
    this.patch({ connection: state, exit });
  }

  private onEvent(event: EventMessage): void {
    switch (event.type) {
      case "hello":
        // A fresh attachment: what this screen sent on an earlier socket is not in force here, so the
        // size it wants is proposed again (and sent only if it may be). One already sent on this
        // socket, ahead of the hello, stands — sending it twice would make the program redraw twice.
        if (!this.sizedThisSocket) this.scheduler.forget();
        this.patch({
          title: event.terminal.title || this.stateValue.title,
          cwd: event.terminal.cwd || this.stateValue.cwd,
          keyboard: event.keyboard,
          altScreen: event.modes.alt_screen,
          exit: event.terminal.status === "running" ? null : this.stateValue.exit,
        });
        // The hello describes the PTY as it was when the attach arrived; a size this socket sent since is
        // on its way and will be confirmed by a `size` event, so the stale one is not drawn meanwhile.
        if (this.sizedThisSocket) this.patch({ size: { cols: event.size.cols, rows: event.size.rows, owner: event.size.owner } });
        else this.applySize(event.size);
        this.fit();
        this.scheduleRefill();
        break;
      case "resync":
        this.cancelRefill();
        this.marks?.resync(event.first_abs_row);
        break;
      case "marks":
        this.cancelRefill();
        this.marks?.apply(event);
        break;
      case "command":
        this.marks?.apply(event);
        break;
      case "size":
        this.applySize(event);
        break;
      case "title":
        this.patch({ title: event.title });
        break;
      case "cwd":
        this.patch({ cwd: event.cwd });
        break;
      case "keyboard":
        this.patch({ keyboard: { owner: event.owner, until: event.until } });
        break;
      case "agent_typing":
        this.patch({ agentTyping: event.active ? event.actor || "agent" : null });
        break;
      case "mode":
        this.patch({ altScreen: event.alt_screen });
        break;
      case "exit":
        this.patch({ exit: { code: event.code, signal: event.signal } });
        break;
      default:
        break;
    }
  }

  /**
   * A reattach that continues from the bytes this terminal holds brings no snapshot and so no `marks`:
   * commands that ran while it was away would have no mark. When no snapshot follows the `hello`, the
   * host is asked for the recent ones. A shell that never marked anything is not asked about.
   */
  private scheduleRefill(): void {
    this.cancelRefill();
    if (!this.marks?.active) return;
    this.refillTimer = setTimeout(() => {
      this.refillTimer = null;
      api.terminalCommands(this.id, 50, false).then(
        ({ commands }) => this.marks?.fill(commands),
        () => undefined,
      );
    }, REFILL_MS);
  }

  private cancelRefill(): void {
    if (this.refillTimer) clearTimeout(this.refillTimer);
    this.refillTimer = null;
  }

  /** The PTY's size, as the daemon announced it: this terminal draws at it whoever chose it. */
  private applySize(size: { cols: number; rows: number; owner: SizeOwner }): void {
    const term = this.term;
    if (term && size.cols > 0 && size.rows > 0 && (term.cols !== size.cols || term.rows !== size.rows)) term.resize(size.cols, size.rows);
    // When another screen takes the size, what this one sent is no longer in force: the next time
    // this screen may claim (focus, a click, "Fit here"), its size goes out again even if unchanged.
    if (size.owner !== "you") this.scheduler.forget();
    this.patch({ size: { cols: size.cols, rows: size.rows, owner: size.owner } });
    this.updateSizedElsewhere();
  }

  private updateSizedElsewhere(): void {
    const size = this.stateValue.size;
    const desired = this.desired;
    const elsewhere = !!size && !!desired && size.owner !== "you" && (desired.cols !== size.cols || desired.rows !== size.rows);
    this.patch({ sizedElsewhere: elsewhere });
  }

  private sendSize(size: Size): boolean {
    const term = this.term;
    const element = term?.element;
    if (!this.connection || !term) return false;
    const width = element?.clientWidth ?? 0;
    const height = element?.clientHeight ?? 0;
    if (!this.connection.resize(size.cols, size.rows, width, height)) return false;
    this.sizedThisSocket = true;
    // Drawn at the new size at once; the daemon's `size` event confirms it (or names another owner).
    if (term.cols !== size.cols || term.rows !== size.rows) term.resize(size.cols, size.rows);
    return true;
  }

  private onKey(e: KeyboardEvent): boolean {
    const term = this.term;
    const action = reservedKey(e, {
      mac: isMac(),
      altScreen: this.stateValue.altScreen || term?.buffer.active.type === "alternate",
      selection: !!term?.hasSelection(),
      mouseProgram: !!term && term.modes.mouseTrackingMode !== "none",
      held: this.heldCopy() !== null,
    });
    if (action === null) return true;
    // Ctrl+↑/↓ move between commands only where the shell marks them; anywhere else they belong to
    // the program, as they always did.
    if ((action === "previous-mark" || action === "next-mark") && !this.marks?.active) return true;
    if (e.type === "keydown") this.act(action, e);
    return false;
  }

  private act(action: TerminalAction, e: KeyboardEvent): void {
    switch (action) {
      case "copy":
        e.preventDefault();
        void this.copySelection();
        break;
      case "copy-clear":
        // Cleared so that a second Ctrl+C is the interrupt again, as it is in VS Code.
        e.preventDefault();
        void this.copySelection();
        this.term?.clearSelection();
        break;
      case "paste":
        // Left to the browser: the key's own paste event arrives at the listener above, which is
        // where the multi-line check lives. Ctrl+Shift+V pastes as plain text in every browser.
        break;
      case "search":
        e.preventDefault();
        this.request({ kind: "search" });
        break;
      case "font-bigger":
      case "font-smaller":
      case "font-reset":
        e.preventDefault();
        fontSizeStep(action);
        break;
      case "previous-mark":
      case "next-mark":
        e.preventDefault();
        this.jumpToCommand(action === "previous-mark" ? -1 : 1);
        break;
      case "toggle-dock":
        // The dock's own listener on the document hears the same key; the terminal only stays out.
        break;
      default:
        break;
    }
  }

  private heldCopy(): string | null {
    if (this.held && Date.now() - this.held.at > HELD_COPY_MS) this.held = null;
    return this.held?.text ?? null;
  }

  /**
   * Shift-drag selects over a program that reports the mouse, on a Mac too. xterm.js forces its own
   * selection there with Option only (`macOptionClickForcesSelection`), while Linux and Windows use
   * Shift — and Claude Code's hint names Shift. So a Shift press on a Mac is handed on as an Option
   * press; the release stays as it was, so a short click does not move the cursor the way an
   * Option-click does.
   */
  private shiftSelectsOnMac(e: MouseEvent): void {
    if (!e.isTrusted || !e.shiftKey || e.altKey || e.button !== 0 || !isMac()) return;
    if (!this.term || this.term.modes.mouseTrackingMode === "none" || !e.target) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    e.target.dispatchEvent(new MouseEvent("mousedown", {
      bubbles: true, cancelable: true, composed: true, view: e.view, detail: e.detail,
      screenX: e.screenX, screenY: e.screenY, clientX: e.clientX, clientY: e.clientY,
      shiftKey: true, altKey: true, ctrlKey: e.ctrlKey, metaKey: e.metaKey, button: e.button, buttons: e.buttons,
    }));
  }

  private onPaste(e: ClipboardEvent): void {
    if (!this.term || this.readOnly) return;
    const text = e.clipboardData?.getData("text/plain") ?? "";
    if (!/[\r\n]/.test(text.replace(/[\r\n]+$/, ""))) return;
    if (this.term.modes.bracketedPasteMode || remembered(PASTE_KEY(this.id))) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    this.request({ kind: "paste", text, lines: text.replace(/[\r\n]+$/, "").split(/\r\n|\r|\n/).length });
  }

  private openUrl(uri: string): void {
    const env = this.shared.env(this.envName);
    const url = env ? rewriteLoopbackUrl(uri, env) : uri;
    window.open(url, "_blank", "noopener,noreferrer");
  }

  /** An OSC 8 hyperlink: web addresses open, a file in the workspace previews, anything else asks first. */
  private openUri(uri: string): void {
    let parsed: URL | null = null;
    try {
      parsed = new URL(uri);
    } catch {
      parsed = null;
    }
    if (parsed && (parsed.protocol === "http:" || parsed.protocol === "https:")) return this.openUrl(uri);
    if (parsed && parsed.protocol === "file:") {
      const rel = resolveFileLink(decodeURIComponent(parsed.pathname), this.stateValue.cwd, this.binding.workspace ?? "");
      if (rel && this.binding.fileOpener) return this.binding.fileOpener(rel);
    }
    this.request({ kind: "link", uri });
  }

  /** The environment this terminal runs in, set by whoever lists it; links are rewritten by its ports. */
  envName = "container";

  private fileLinks(y: number): ILink[] | undefined {
    const term = this.term;
    const opener = this.binding.fileOpener;
    const workspace = this.binding.workspace;
    if (!term || !opener || !workspace) return undefined;
    const line = term.buffer.active.getLine(y - 1);
    if (!line) return undefined;
    const { text, cellOf } = lineCells(line);
    const cwd = this.stateValue.cwd || workspace;
    const links: ILink[] = [];
    for (const found of findFileLinks(text)) {
      const rel = resolveFileLink(found.path, cwd, workspace);
      if (!rel) continue;
      const start = cellOf[found.start];
      const end = cellOf[found.end - 1];
      if (start === undefined || end === undefined) continue;
      links.push({
        range: { start: { x: start + 1, y }, end: { x: end + 1, y } },
        text: text.slice(found.start, found.end),
        decorations: { underline: true, pointerCursor: true },
        activate: () => opener(rel, found.line),
      });
    }
    return links;
  }
}

// ── the font size, one per device ─────────────────────────────────────────────────────────────

export const FONT_KEY = "daedalus.term.font";
export const FONT_DEFAULT = 13;
export const FONT_MIN = 9;
export const FONT_MAX = 24;

const fontListeners = new Set<() => void>();

export function storedFontSize(): number {
  try {
    const n = Number(localStorage.getItem(FONT_KEY));
    return Number.isFinite(n) && n >= FONT_MIN && n <= FONT_MAX ? n : FONT_DEFAULT;
  } catch {
    return FONT_DEFAULT;
  }
}

export function fontSizeStep(action: "font-bigger" | "font-smaller" | "font-reset"): number {
  const current = storedFontSize();
  const next = action === "font-reset" ? FONT_DEFAULT : Math.max(FONT_MIN, Math.min(FONT_MAX, current + (action === "font-bigger" ? 1 : -1)));
  try {
    localStorage.setItem(FONT_KEY, String(next));
  } catch {
    /* kept for this page only */
  }
  fontListeners.forEach((l) => l());
  return next;
}

/** Set the device's font size outright (a pinch ends on one), within the bounds the steps keep to. */
export function setFontSize(size: number): number {
  const next = Math.max(FONT_MIN, Math.min(FONT_MAX, Math.round(size)));
  if (next === storedFontSize()) return next;
  try {
    localStorage.setItem(FONT_KEY, String(next));
  } catch {
    /* kept for this page only */
  }
  fontListeners.forEach((l) => l());
  return next;
}

export function onFontSize(listener: () => void): () => void {
  fontListeners.add(listener);
  return () => fontListeners.delete(listener);
}

/** Remember that multi-line pastes into this terminal need no question. */
export function rememberPaste(id: string): void {
  try {
    localStorage.setItem(PASTE_KEY(id), "1");
  } catch {
    /* asked again next time */
  }
}
