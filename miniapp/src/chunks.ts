// The screens that load as chunks of their own, and the two things a chunk needs beyond `lazy`:
// retrying a download that failed, and rendering at once once it has arrived.

import { type ComponentType, type LazyExoticComponent, lazy } from "react";

/** The module a failed dynamic import names, the way Chromium ("Failed to fetch dynamically imported
 *  module: <url>") and Firefox ("error loading dynamically imported module: <url>") word it, without
 *  its query. Safari's message names none. */
export function failedModule(error: unknown): string | null {
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const found = /(https?:\/\/[^\s'"?#]+?\.m?js)(?=[?#\s'"]|$)/.exec(message);
  return found ? found[1] : null;
}

/** A dynamic import retried a couple of times before it gives up, and the part of the module wanted.
 *
 *  `lazy` remembers the promise it was given, rejection included, so a chunk that failed to arrive
 *  once never arrives at all: re-rendering the screen replays the same rejection and only a reload
 *  recovers. The target here is a phone on a flaky link, where a failed chunk is a normal event and
 *  not a broken build, so the import is retried before the boundary sees it.
 *
 *  A retry asks for the file under another address. Chromium keeps a module whose download failed as
 *  failed for the rest of the page's life, and `import()` of the same address rejects again at once
 *  without a request: calling the loader again, as this once did, never reached the network, and one
 *  dropped request drew "This screen belongs to an older version of the app". The address is taken
 *  from the error and asked for with `?retry=n`; the module's own imports resolve to their usual
 *  addresses, so the shared chunks already loaded stay the same instances. Where the error names no
 *  address, the loader is called again, which is all a browser that does not keep the failure needs.
 *  A build whose files are gone fails every time and still ends on the notice. */
export function retried<M, T>(load: () => Promise<M>, pick: (module: M) => T, again: (url: string) => Promise<unknown> = (url) => import(/* @vite-ignore */ url)): () => Promise<T> {
  return async () => {
    let error: unknown;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) await new Promise((r) => setTimeout(r, 400 * attempt));
      const url = attempt > 0 ? failedModule(error) : null;
      try {
        return pick(url ? ((await again(`${url}?retry=${attempt}`)) as M) : await load());
      } catch (e) {
        error = e;
      }
    }
    throw error;
  };
}

export type Chunk<T extends ComponentType<any>> = LazyExoticComponent<T> & { prefetch: () => void };

/** `lazy`, plus a prefetch after which the first render does not suspend.
 *
 *  A plain `lazy` suspends on its first render even when the module arrived long ago: it is handed a
 *  promise, and a promise answers only on a later tick, by which time the nearest Suspense boundary
 *  has already drawn its fallback. That fallback is what made opening a project from the main chat
 *  empty the column and put "Loading…" where the conversation was for a frame. Once fetched, the
 *  module is handed over through a thenable that answers at once, which `lazy` reads in the same
 *  render. */
export function chunk<T extends ComponentType<any>>(load: () => Promise<{ default: T }>): Chunk<T> {
  let module: { default: T } | null = null;
  let pending: Promise<{ default: T }> | null = null;
  const fetch = () => {
    pending ??= load().then(
      (m) => (module = m),
      (e) => {
        pending = null; // the next render or prefetch tries again
        throw e;
      },
    );
    return pending;
  };
  const component = lazy(() => {
    const ready = module;
    if (!ready) return fetch();
    const now = { then: (resolve: (m: { default: T }) => void) => resolve(ready) };
    return now as unknown as Promise<{ default: T }>;
  });
  return Object.assign(component, { prefetch: () => void fetch().catch(() => undefined) }); // a prefetch that fails is not an error: the render retries
}

/** The conversation: the agents list's, the main chat's and a project's, one chunk and one component,
 *  so fetching it once readies all three. */
export const SessionScreen = chunk(retried(() => import("./screens/Session"), (m) => ({ default: m.SessionScreen })));

/** Work for the browser's idle time: what the operator is likely to open next. */
export function whenIdle(work: () => void): void {
  const idle = (window as { requestIdleCallback?: (cb: () => void) => void }).requestIdleCallback;
  if (idle) idle(work);
  else window.setTimeout(work, 2000);
}

/** What a chunk that would not load, after its retries, means: the build moved under the page, or the
 *  connection dropped while it was downloading.
 *
 *  A retry under another address recovers a screen's own file, but not a file it shares with other
 *  screens that failed alongside it: that one is imported by its fixed address and stays failed until
 *  the page is loaded again. Such a failure used to be reported as "an older version of the app",
 *  which a dropped connection is not. The server is asked whether the file is still there: if it is,
 *  the build is the current one and a reload recovers everything; if it is gone, the notice is true. */
export async function chunkVerdict(error: unknown, ask: (url: string) => Promise<Response> = (url) => fetch(url, { method: "HEAD", cache: "no-store" })): Promise<"dropped" | "stale" | "offline"> {
  const url = failedModule(error);
  if (!url) return "stale"; // a browser that names no file: nothing to ask about, as before
  try {
    const answer = await ask(url);
    return answer.ok ? "dropped" : "stale";
  } catch {
    return "offline";
  }
}

const RELOADED_KEY = "daedalus.chunk.reloaded";

/** Whether this page may reload itself for a dropped download: once a minute at most, so a file that
 *  answers the question but never loads ends on the notice instead of reloading forever. */
export function mayReload(storage: Pick<Storage, "getItem" | "setItem"> | null, now = Date.now()): boolean {
  // Without storage a reload could not be counted, and one that recurs would never stop: none is made.
  if (!storage) return false;
  try {
    const last = Number(storage.getItem(RELOADED_KEY) ?? 0);
    if (now - last < 60_000) return false;
    storage.setItem(RELOADED_KEY, String(now));
    return true;
  } catch {
    return false;
  }
}
