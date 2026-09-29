// The Harnesses screen as the app decides it (M7): what each command-line agent's row says and which
// button it offers, how many updates wait, and what a refused update names. Pure, so each rule is
// tested without a browser. The rows come from the harness manager (`GET /api/harnesses`), which
// already joined the capability table with the last check: nothing here names a particular CLI.

export type SelfCheckStep = { name: string; ok: boolean; skipped?: boolean; detail?: string; duration_ms?: number };
export type SelfCheck = { ok: boolean; version?: string; at?: string; duration_ms?: number; steps: SelfCheckStep[] };
export type HarnessOperation = { kind: "check" | "update" | "install" | "signin"; started_at: string; terminal_id: string | null; target: string };

export type HarnessRow = {
  env: string;
  harness: string;
  label: string;
  installed: boolean;
  installed_version: string;
  latest_version: string;
  install_method: string;
  logged_in: "yes" | "no" | "unknown";
  login_detail: string;
  agents: { name: string; source?: string; description?: string }[];
  /** What is offered: the operator's choice, or every model when there is none. */
  models: string[];
  /** Every model the CLI listed; any of them can still be named on a hire. */
  all_models: string[];
  models_chosen: boolean;
  self_check: SelfCheck | Record<string, never>;
  checked_at: string | null;
  error: string;
  status_channel: string;
  status_channel_label: string;
  tested_versions: [string, string];
  tested: boolean;
  supported: boolean;
  adapter: boolean;
  version_guard: "" | "verified" | "unverified";
  update_available: boolean;
  operation: HarnessOperation | null;
  installable: boolean;
  install_problem: string;
  can_sign_in: boolean;
  unavailable: string;
};

export type NodeState = { installed: boolean; version: string; path: string; pinned: string; current: boolean; checked_at: string | null };
export type HarnessScreen = { env: "container" | "host"; environments: string[]; rows: HarnessRow[]; node: NodeState; checked_at: string | null; updates: number };

/** The one thing a row offers, in the order that matters: something is running on it; it is not
 *  there (install, or say why it cannot be); it cannot sign in; a newer version waits; it is current. */
export type RowState = "busy" | "install" | "blocked" | "signin" | "update" | "current";

export function rowState(row: Pick<HarnessRow, "operation" | "installed" | "installable" | "logged_in" | "can_sign_in" | "update_available">): RowState {
  if (row.operation) return "busy";
  if (!row.installed) return row.installable ? "install" : "blocked";
  if (row.logged_in === "no" && row.can_sign_in) return "signin";
  if (row.update_available) return "update";
  return "current";
}

/** How many CLIs have a newer version waiting: the header's "N updates", and whether Update all does anything. */
export function updateCount(rows: Pick<HarnessRow, "installed" | "update_available" | "operation">[]): number {
  return rows.filter((r) => r.installed && r.update_available && !r.operation).length;
}

/** The sign-in cell's key and words: the CLI's own description of the account where it gave one. */
export function signIn(row: Pick<HarnessRow, "installed" | "logged_in" | "login_detail">): { key: string; detail: string; tone: "ok" | "warn" | "off" } {
  if (!row.installed) return { key: "harness.signin.absent", detail: "", tone: "off" };
  if (row.logged_in === "yes") return { key: row.login_detail ? "harness.signin.as" : "harness.signin.yes", detail: row.login_detail, tone: "ok" };
  if (row.logged_in === "no") return { key: "harness.signin.no", detail: "", tone: "warn" };
  return { key: "harness.signin.unknown", detail: "", tone: "off" };
}

/** The version marker: nothing inside the tested range; "untested" outside it until a self-check
 *  passes on that very version, and then "untested, self-checked". */
export function versionMark(row: Pick<HarnessRow, "installed" | "version_guard" | "supported">): "" | "unsupported" | "unverified" | "verified" {
  if (!row.installed) return "";
  if (!row.supported) return "unsupported";
  return row.version_guard;
}

/** The last self-check in one line: passed, failed at a step, or never run; skipped steps counted apart. */
export function checkSummary(check: SelfCheck | Record<string, never> | null | undefined): { key: string; step: string; skipped: number } {
  if (!check || !("steps" in check) || !Array.isArray(check.steps)) return { key: "harness.check.never", step: "", skipped: 0 };
  const skipped = check.steps.filter((s) => s.skipped).length;
  if (check.ok) return { key: skipped ? "harness.check.passed.skipped" : "harness.check.passed", step: "", skipped };
  const failed = check.steps.find((s) => !s.ok && !s.skipped);
  return { key: "harness.check.failed", step: failed?.name ?? "", skipped };
}

/** Who a refused update names: the staff working on that CLI, "Ira (Bakery 2.0)". */
export function refusedStaff(data: Record<string, unknown> | undefined): string[] {
  const staff = Array.isArray(data?.staff) ? (data!.staff as unknown[]) : [];
  return staff.flatMap((s) => {
    if (!s || typeof s !== "object") return [];
    const row = s as Record<string, unknown>;
    const name = typeof row.name === "string" ? row.name : "";
    const project = typeof row.project === "string" ? row.project : "";
    return name ? [project ? `${name} (${project})` : name] : [];
  });
}

/** Where an agent comes from, as M7 writes it: a folder's own, the user's, or the CLI's built-in one. */
export function agentSource(source: string | undefined): string {
  if (source === "project") return "harness.agent.project";
  if (source === "builtin") return "harness.agent.builtin";
  return "harness.agent.user";
}

/** The checklist a row's "Models to offer" starts from: the chosen models, or nothing ticked when
 *  there is no choice. Picking a few out of many is the point, so the operator ticks the few rather
 *  than unticking the many. */
export function offeredDraft(row: Pick<HarnessRow, "models" | "all_models" | "models_chosen">): string[] {
  return row.models_chosen ? row.models.filter((m) => row.all_models.includes(m)) : [];
}

/** What a save sends: null — offer everything — when nothing or everything is ticked, since offering
 *  nothing would leave the hiring form without a list and offering all is no choice at all. */
export function offeredToSend(draft: string[], all: string[]): string[] | null {
  const ticked = all.filter((m) => draft.includes(m));
  return ticked.length === 0 || ticked.length === all.length ? null : ticked;
}
