// What the "Import GitHub issues" sheet shows and sends, kept apart from the sheet so the rules about
// which issues can be ticked and what is sent for them are tested without a browser.

export type IssueAction = "import" | "update" | "linked" | "conflict" | "too_large" | "elsewhere";

export type IssueEntry = {
  number: number;
  title: string;
  url: string;
  labels: string[];
  body_length: number;
  action: IssueAction;
  task_id: string | null;
  task_title: string | null;
  preview_digest: string | null;
  expected_entity_revision: number | null;
};

export type IssueListing = {
  project_id: string;
  repository: string;
  label: string | null;
  collection_revision: number;
  limit: number;
  issues: IssueEntry[];
};

export type IssueSource = { project_id: string; repository: string | null; configured: boolean };

export type SelectionItem = { issue_number: number; action: "import" | "update"; preview_digest: string; expected_entity_revision: number | null };

export type ImportResult = {
  applied: { issue_number: number; action: "import" | "update"; task_id: string }[];
  skipped: { issue_number: number; reason: string }[];
  collection_revision: number;
};

/** The same shape the host accepts; checked here so a typo is caught before a request is spent on it. */
export function validRepository(text: string): boolean {
  const value = text.trim();
  return /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/.test(value) && !value.split("/").some((part) => part === "." || part === "..");
}

/** A pasted ``https://github.com/o/r`` or ``git@github.com:o/r.git`` is read as the ``o/r`` it names. */
export function normalizeRepository(text: string): string {
  const value = text.trim();
  const match = /github\.com[:/]([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/.exec(value);
  return match ? `${match[1]}/${match[2]}` : value;
}

/** Only an issue that would become a card, or update the card it already has, can be ticked. */
export function selectable(entry: IssueEntry): boolean {
  return (entry.action === "import" || entry.action === "update") && !!entry.preview_digest;
}

/** The ticked entries in the listing's order, which is the order the host applies them in. */
export function selectionItems(listing: IssueListing, picked: ReadonlySet<number>): SelectionItem[] {
  return listing.issues
    .filter((entry) => picked.has(entry.number) && selectable(entry))
    .map((entry) => ({
      issue_number: entry.number,
      action: entry.action as "import" | "update",
      preview_digest: entry.preview_digest!,
      expected_entity_revision: entry.expected_entity_revision,
    }));
}

/** Tick every selectable entry, or clear them all when every one is already ticked. */
export function toggleAll(listing: IssueListing, picked: ReadonlySet<number>): Set<number> {
  const all = listing.issues.filter(selectable).map((entry) => entry.number);
  return all.every((number) => picked.has(number)) ? new Set() : new Set(all);
}

export function counts(listing: IssueListing): Record<IssueAction, number> {
  const out: Record<IssueAction, number> = { import: 0, update: 0, linked: 0, conflict: 0, too_large: 0, elsewhere: 0 };
  for (const entry of listing.issues) out[entry.action] += 1;
  return out;
}

/** What the sheet keeps ticked after a new listing: only numbers that are still selectable there. */
export function keepPicked(listing: IssueListing, picked: ReadonlySet<number>): Set<number> {
  return new Set(listing.issues.filter((entry) => picked.has(entry.number) && selectable(entry)).map((entry) => entry.number));
}
