// The page's terminals: one registry for every view (dock, full screen, the Terminals screen), and the
// few things all of them share — the environments links are rewritten for, the font size, the
// colour scheme.

import type { TerminalEnv } from "../api";
import { InstanceShared, onFontSize, storedFontSize, TerminalInstance } from "./instance";
import { TerminalRegistry } from "./registry";

const envs = new Map<string, TerminalEnv>();
const live = new Set<TerminalInstance>();
let webgl2: boolean | null = null;

function domOnly(): boolean {
  try {
    return localStorage.getItem("daedalus.term.renderer") === "dom";
  } catch {
    return false;
  }
}

const shared: InstanceShared = {
  env: (name) => envs.get(name),
  fontSize: storedFontSize,
  webgl2: () => {
    if (webgl2 === null) {
      try {
        // `daedalus.term.renderer = dom` turns WebGL off on this device: the way out for a GPU that
        // draws a terminal wrongly, and for the screenshot run, whose emulated pixel ratio Chromium
        // does not report in the device-pixel box the WebGL canvas is sized from.
        webgl2 = !domOnly() && !!document.createElement("canvas").getContext("webgl2");
      } catch {
        webgl2 = false;
      }
    }
    return webgl2;
  },
};

/** Options a view asks for when it is the first to open a terminal. */
const pending = new Map<string, { readOnly: boolean }>();

export const terminals = new TerminalRegistry<TerminalInstance>({
  create: (id) => {
    const instance = new TerminalInstance(id, shared, pending.get(id)?.readOnly ?? false);
    live.add(instance);
    return instance;
  },
  dispose: (instance) => {
    live.delete(instance);
    instance.dispose();
  },
  sleep: (instance) => instance.sleep(),
  wake: (instance) => instance.wake(),
  webgl: (instance, on) => instance.setWebgl(on),
});

/** `terminals.acquire`, with the options the first view wants. */
export function acquireTerminal(id: string, options: { readOnly?: boolean; env?: string } = {}): TerminalInstance {
  pending.set(id, { readOnly: !!options.readOnly });
  const instance = terminals.acquire(id);
  pending.delete(id);
  if (options.env) instance.envName = options.env;
  return instance;
}

/** The open instance for a terminal, if any view has it. */
export function instanceFor(id: string): TerminalInstance | undefined {
  for (const instance of live) if (instance.id === id) return instance;
  return undefined;
}

/** The environments as the host last listed them. */
export function setTerminalEnvs(list: TerminalEnv[]): void {
  for (const env of list) envs.set(env.env, env);
}

onFontSize(() => live.forEach((instance) => instance.applyFontSize()));

// The scheme can change under an open terminal (the system's, Telegram's, the app's own switch); the
// terminal's colours are read from the same tokens, so they are read again.
if (typeof window !== "undefined" && typeof MutationObserver !== "undefined") {
  const again = () => live.forEach((instance) => instance.applyTheme());
  new MutationObserver(again).observe(document.documentElement, { attributes: true, attributeFilter: ["data-scheme", "data-tg", "style", "class"] });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", again);
}

// The browser checks read what a terminal holds — its text, its size, its renderer — without
// depending on how xterm.js paints (WebGL leaves no text in the document). Only a page that asked
// for it gets the hook, through a flag nobody sets by accident.
type DebugHook = {
  ids(): string[];
  text(id: string): string[];
  size(id: string): { cols: number; rows: number } | null;
  renderer(id: string): string | null;
  baseY(id: string): number;
  viewportY(id: string): number;
  marks(id: string): { n: number; line: number; result: string }[];
  webglContexts(): number;
  /** Hand xterm.js a chunk as a phone's keyboard would, so it reaches the input filters. */
  feed(id: string, data: string): void;
  fontSize(id: string): number;
  /** Line feeds xterm.js has parsed since the first call for this terminal: output parsed twice shows here. */
  lineFeeds(id: string): number;
  /** Parse data as if the program wrote it, straight into xterm.js: for output a check must time itself. */
  write(id: string, data: string): void;
  /** What xterm.js has selected: whether a copy key would copy, and whether a copy cleared it. */
  selection(id: string): string;
};

const lineFeedCounts = new Map<string, { count: number }>();

try {
  if (typeof window !== "undefined" && localStorage.getItem("daedalus.debug.terminals") === "1") {
    const hook: DebugHook = {
      ids: () => [...live].map((i) => i.id),
      text: (id) => {
        const buffer = instanceFor(id)?.terminal?.buffer.active;
        if (!buffer) return [];
        const out: string[] = [];
        for (let y = 0; y < buffer.length; y++) out.push(buffer.getLine(y)?.translateToString(true) ?? "");
        return out;
      },
      size: (id) => {
        const term = instanceFor(id)?.terminal;
        return term ? { cols: term.cols, rows: term.rows } : null;
      },
      renderer: (id) => instanceFor(id)?.rendererKind ?? null,
      baseY: (id) => instanceFor(id)?.terminal?.buffer.active.baseY ?? 0,
      viewportY: (id) => instanceFor(id)?.terminal?.buffer.active.viewportY ?? 0,
      marks: (id) => instanceFor(id)?.commandMarks?.describe() ?? [],
      webglContexts: () => [...live].filter((i) => i.rendererKind === "webgl").length,
      feed: (id, data) => instanceFor(id)?.terminal?.input(data, true),
      fontSize: (id) => instanceFor(id)?.terminal?.options.fontSize ?? 0,
      write: (id, data) => instanceFor(id)?.terminal?.write(data),
      selection: (id) => instanceFor(id)?.terminal?.getSelection() ?? "",
      lineFeeds: (id) => {
        let counter = lineFeedCounts.get(id);
        if (!counter) {
          const term = instanceFor(id)?.terminal;
          if (!term) return 0;
          const fresh = { count: 0 };
          term.onLineFeed(() => fresh.count++);
          lineFeedCounts.set(id, fresh);
          counter = fresh;
        }
        return counter.count;
      },
    };
    (window as unknown as { __terminals: DebugHook }).__terminals = hook;
  }
} catch {
  /* storage refused: no hook */
}
