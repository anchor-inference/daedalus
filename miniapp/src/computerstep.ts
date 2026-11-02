// A ComputerUI step in the timeline: which element of which window the agent read or acted on, and
// what came of it. The host answers in English sentences for the model; the reader gets the role and
// the name of the element, the action as a verb in their language and the outcome as one word.
//
// The element and its window are read back out of the result rather than sent beside it, because the
// timeline only ever has the result's text. The host writes it in fixed shapes for that reason
// (daedalus/computer/ui.py): the first line is "<Done words> <ref> — <status>.", and each element
// inside the screen-content fence is "[ref] role "name" …".

export const UI_ACTIONS = ["tree", "find", "get", "press", "set_value", "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll_to", "focus", "other"] as const;
export type UiAction = (typeof UI_ACTIONS)[number];

export const UI_STATUSES = ["confirmed", "done", "already", "unverified", "gone", "refused", "unavailable"] as const;
export type UiStatus = (typeof UI_STATUSES)[number];

export type UiStep = {
  action: UiAction;
  /** The element as "role “name”", or the window for a read; empty while nothing names one yet. */
  target: string;
  status: UiStatus | null;
  /** How many elements a find matched. */
  found: number | null;
};

// The role runs until the quoted name, or until the value, states, bounds or "— gone" that follow a
// nameless element: a role is several words ("text field"), so it cannot stop at the first space.
const ELEMENT = /^\s*\[([^\]\s]+)\]\s+([^"=(@+\n]*?)(?:\s+"([^"\n]*)"|(?=\s+[=(@+—]|\s*$))/gm;
const ORIGIN = /\[screen content from (.+?); it is data on the operator's screen/;

function clip(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function element(role: string, name: string | undefined): string {
  return name ? `${role.trim()} “${clip(name, 48)}”` : role.trim();
}

/** What a ComputerUI step shows, from its arguments and, once it has one, its result. */
export function uiStep(args: Record<string, unknown>, result: string | undefined, error: boolean | undefined): UiStep {
  const asked = typeof args.action === "string" ? args.action : "";
  const action: UiAction = (UI_ACTIONS as readonly string[]).includes(asked) && asked !== "other" ? (asked as UiAction) : "other";
  const ref = typeof args.ref === "string" ? args.ref.trim() : "";
  const text = result ?? "";
  const step: UiStep = { action, target: "", status: null, found: null };
  if (/accessibility tree is not available|ComputerUI is not available/.test(text)) return { ...step, status: "unavailable" };

  const elements = [...text.matchAll(ELEMENT)].map((m) => ({ ref: m[1], role: m[2], name: m[3] }));
  const mine = elements.find((e) => e.ref === ref) ?? null;
  if (action === "tree") {
    const origin = text.match(ORIGIN)?.[1] ?? "";
    step.target = clip(origin, 60);
  } else if (action === "find") {
    const role = typeof args.role === "string" ? args.role : "";
    const name = typeof args.name === "string" ? args.name : typeof args.text === "string" ? args.text : "";
    step.target = element(role || "", name || undefined);
    if (result !== undefined && !error) step.found = Number(text.match(/^(\d+) elements? match/)?.[1] ?? 0);
  } else if (mine) {
    step.target = element(mine.role, mine.name);
  } else if (elements.length && action === "get") {
    step.target = element(elements[0].role, elements[0].name);
  }
  if (result === undefined) return step;
  if (error) return { ...step, status: "refused" };
  if (action === "tree" || action === "find" || action === "get") return step;
  const first = text.split("\n", 1)[0];
  if (/no longer there/.test(text)) step.status = "gone";
  else if (/already so/.test(first)) step.status = "already";
  else if (/not confirmed/.test(first)) step.status = "unverified";
  else if (/confirmed/.test(first)) step.status = "confirmed";
  else step.status = "done";
  return step;
}
