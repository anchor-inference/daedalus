// What this installation can do, from GET /api/capabilities. The app offers what is there and
// nothing else: a destination that answers 404 is worse than a destination that is not in the nav.

import { t } from "./i18n";
import type { Screen } from "./router";

export type SelfDevMode = "off" | "local" | "server";

/** A change the agent committed to the checkout, waiting for the restart that runs it. */
export type PendingChange = { repo: string; commit: string; summary: string; needs_image?: boolean; at?: string };

/** What became of the change before it. `status` is the supervisor's word, not ours. */
export type ChangeResult = PendingChange & { status: string; detail?: string; resolved_at?: string };

/** The short form of the components registry, carried here so the shell needs no second poll. */
export type ComponentsSummary = {
  mode: string;
  /** Every optional piece this installation does not have. */
  missing: string[];
  /** The sharp end: the pieces something *already configured* needs and does not have. A local voice
   *  chosen with no runtime to speak it is the case this exists for — the page looked configured. */
  needed: string[];
  installing: string;
};

export type Capabilities = {
  selfdev: { mode: SelfDevMode; configured: string; reasons: string[]; missing: string[]; tools: string[] };
  restart_required?: PendingChange | null;
  last_change?: ChangeResult | null;
  components?: ComponentsSummary | null;
  /** Whether a folder or a file may be shown in the operator's own file manager, and which one. */
  reveal?: { available: boolean; platform: "windows" | "macos" | "linux" } | null;
};

export type Notice = { kind: "pending" | "done" | "failed"; title: string; body: string; commit: string; action: boolean };

/** What the app has to say about the agent's own code right now, or nothing.
 *
 * A change waiting to be applied wins over one that has already been decided: the older result is
 * history the moment a new change is sitting there, and two strips stacked on top of each other is
 * one strip too many. A result is shown until the reader dismisses it — "restart to apply" that
 * simply vanishes leaves nobody sure whether the restart did anything.
 */
export function changeNotice(caps: Capabilities | undefined, dismissed: string): Notice | null {
  const pending = caps?.restart_required;
  if (pending?.commit) {
    const image = pending.needs_image ? t("change.pending.image") : "";
    return { kind: "pending", title: t("change.pending.title"), body: pending.summary + image, commit: pending.commit, action: true };
  }
  const last = caps?.last_change;
  if (!last?.commit || last.commit === dismissed) return null;
  if (last.status === "applied") {
    return { kind: "done", title: t("change.done.title"), body: last.summary, commit: last.commit, action: false };
  }
  const title = t(last.status === "rolled_back" ? "change.reversed.title" : "change.failed.title");
  return { kind: "failed", title, body: last.detail || last.summary, commit: last.commit, action: false };
}

/** Whether the Components entry should carry a mark: something configured is missing its runtime.
 *
 * Only ``needed``, never ``missing``. A portable installation is missing most of the list by design
 * and a permanent mark beside a settings entry says nothing; a mark that appears the day a chosen
 * voice has nothing to speak it is worth looking at.
 */
export function componentsNeedAttention(caps: Capabilities | undefined): boolean {
  return (caps?.components?.needed?.length ?? 0) > 0;
}

/** The destinations this installation really has. */
export function visibleScreens(list: Screen[], selfdev: SelfDevMode): Screen[] {
  return selfdev === "off" ? list.filter((s) => s !== "changes") : list;
}

/** The word beside a destination's name when it works differently here than the name suggests.
 *
 *  A key in the table rather than the word: the rail renders it, and the rail is read in two
 *  languages.
 */
export function screenTag(s: Screen, selfdev: SelfDevMode, beta: Screen[]): string {
  if (s === "changes" && selfdev === "local") return "nav.tag.local";
  return beta.includes(s) ? "nav.tag.beta" : "";
}
