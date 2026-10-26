// The review of what the background pass held for the operator's word: grouped by the entry each
// suggestion is for or by the pass (one chat read once) that filed it, each group accepted or dismissed
// whole as one change with Undo, and every row showing the chat it came from and what was said there.

import { useMemo, useState } from "react";
import { api, type MemoryDecision, type MemoryRecord, type MemorySuggestion, type MemorySuggestions } from "../api";
import { relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { sessionPath } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { Sheet, toast } from "../ui/dialogs";
import { Segmented, Skeleton, go } from "../ui/index";
import { REVIEW_BY, groupSuggestions, suggestionsFor, type ReviewBy, type SuggestionGroup } from "./model";
import { undoToast } from "./undo";

const KEY = "/api/memory/suggestions";

function refreshAll(): void {
  invalidate("/api/memory");
}

/** Why the pass left this to the operator, in the page's language; a proposed removal gives the model's
 *  own reason (it writes in the chat's language). */
export function learnedWhy(s: MemorySuggestion): string {
  if (s.why === "locked") return t("memory.suggest.why.locked");
  if (s.why === "ungrounded") return t("memory.suggest.why.ungrounded", { names: (s.tokens ?? []).join(", ") });
  if (s.why === "full") return t("memory.suggest.why.full");
  if (s.why === "chat_full") return t("memory.suggest.why.chat_full");
  return s.reason;
}

function kindKey(s: MemorySuggestion): string {
  return s.kind === "revision" ? `revision.${s.action === "retire" ? "retire" : "update"}` : s.kind;
}

function applyKey(s: MemorySuggestion): string {
  return s.kind === "revision" ? (s.action === "retire" ? "retire" : "revision") : s.kind;
}

/** Where a suggestion was learned: the chat, opened at the message it rests on, while the chat exists. */
function ChatLink({ s }: { s: MemorySuggestion }) {
  if (!s.session_id) return null;
  if (!s.session_title) return <span className="memory-source" data-source-gone><Icon name="compose" size={12} /> <span className="memory-source-t">{t("memory.source.gone")}</span></span>;
  const href = `${sessionPath(s.session_id)}${s.from_seq ? `#m${s.from_seq}` : ""}`;
  return <a className="memory-source" href={href} onClick={(e) => go(e, href)} data-source={s.session_id}><Icon name="compose" size={12} /> <span className="memory-source-t">{s.session_title}</span></a>;
}

function Row({ s, record, by, busy, onAct }: { s: MemorySuggestion; record: MemoryRecord | undefined; by: ReviewBy; busy: boolean; onAct: (s: MemorySuggestion, action: "apply" | "dismiss") => void }) {
  return (
    <li className="memory-review-row" data-suggestion={s.id} data-kind={s.kind}>
      <div className="memory-review-kind">{t(`memory.suggest.${kindKey(s)}`)}{by === "pass" && s.entry_title ? <span className="faint"> · {s.entry_title}</span> : null}</div>
      {record && <div className={`memory-text ${s.action === "retire" ? "" : "memory-review-before"}`}>{record.text}</div>}
      {s.action !== "retire" && <div className="memory-text memory-review-text">{s.text}</div>}
      <div className="memory-review-meta">
        <ChatLink s={s} />
        {s.pass_at && <span className="faint">{relTime(s.pass_at)}</span>}
      </div>
      {s.quote && <blockquote className="memory-review-quote" data-quote>{s.quote}</blockquote>}
      <div className="sub memory-review-why">{learnedWhy(s)}</div>
      <div className="memory-review-actions">
        <button className="btn small ghost" data-action="dismiss-one" disabled={busy} onClick={() => onAct(s, "dismiss")}>{t("memory.suggest.dismiss.learned")}</button>
        <button className="btn small primary" data-action="apply-one" disabled={busy} onClick={() => onAct(s, "apply")}>{t(`memory.suggest.apply.${applyKey(s)}`)}</button>
      </div>
    </li>
  );
}

function GroupHead({ group, busy, onAll }: { group: SuggestionGroup; busy: boolean; onAll: (group: SuggestionGroup, action: "apply" | "dismiss") => void }) {
  return (
    <div className="memory-review-head">
      <span className="memory-review-title">{group.title}</span>
      <span className="memory-badge" data-count={group.items.length}>{group.items.length}</span>
      <span className="grow" />
      <span className="memory-review-bulk">
        <button className="btn small ghost" data-action="dismiss-all" disabled={busy} onClick={() => onAll(group, "dismiss")}>{t("memory.review.dismissall")}</button>
        <button className="btn small" data-action="accept-all" disabled={busy} onClick={() => onAll(group, "apply")}>{t("memory.review.acceptall")}</button>
      </span>
    </div>
  );
}

/** The review sheet; ``entryId`` narrows it to one entry's suggestions (opened from that entry). */
export function ReviewSheet({ entryId, onClose, onChanged }: { entryId?: string | null; onClose: () => void; onChanged: () => void }) {
  const { data, error, loading, refresh } = useQuery<MemorySuggestions>(KEY, { staleMs: 0 });
  const [by, setBy] = useState<ReviewBy>("entry");
  const [busy, setBusy] = useState(false);
  const list = useMemo(() => suggestionsFor(data?.suggestions ?? [], entryId), [data, entryId]);
  const groups = useMemo(() => groupSuggestions(list, by), [list, by]);
  const changed = () => { refresh(); refreshAll(); onChanged(); };

  async function run(work: () => Promise<{ said: string; history: number | null | undefined }>) {
    setBusy(true);
    try {
      const { said, history } = await work();
      undoToast(said, history, changed);
      changed();
    } catch (e) {
      toast(errorText(e));
      refresh();
    } finally {
      setBusy(false);
    }
  }

  const onAll = (group: SuggestionGroup, action: "apply" | "dismiss") => void run(async () => {
    const res = await api.post<MemoryDecision>("/api/memory/suggestions/decide", { action, ids: group.bulk });
    const said = action === "apply" ? plural("memory.review.accepted", res.applied) : plural("memory.review.dismissed", res.dismissed);
    return { said: res.stale ? `${said} · ${plural("memory.review.stale", res.stale)}` : said, history: res.history_id };
  });

  const onAct = (s: MemorySuggestion, action: "apply" | "dismiss") => void run(async () => {
    if (action === "dismiss") {
      const res = await api.post<{ dismissed: boolean; history_id: number | null }>(`/api/memory/suggestions/${s.id}/dismiss`, {});
      return { said: plural("memory.review.dismissed", res.dismissed ? 1 : 0), history: res.history_id };
    }
    const res = await api.post<{ history_id: number | null }>(`/api/memory/suggestions/${s.id}/apply`, {});
    return { said: plural("memory.review.accepted", 1), history: res.history_id };
  });

  return (
    <Sheet title={t("memory.review.title")} onClose={onClose} size="wide" className="memory-review">
      <div className="sub">{t("memory.review.sub")}</div>
      {!entryId && list.length > 0 && (
        <div className="memory-review-by">
          <Segmented label={t("memory.review.by")} value={by} onChange={setBy} options={REVIEW_BY.map((id) => ({ id, label: t(`memory.review.by.${id}`) }))} />
        </div>
      )}
      {loading && !data && <Skeleton rows={4} />}
      {error && !data && <div className="empty">{error}</div>}
      {data && list.length === 0 && <div className="empty" data-review-empty><b>{t("memory.review.empty")}</b></div>}
      {groups.map((group) => (
        <section key={group.key} className="memory-review-group" data-group={group.key}>
          <GroupHead group={group} busy={busy} onAll={onAll} />
          <ul className="memory-review-list">
            {group.items.map((s) => <Row key={s.id} s={s} record={s.kind === "revision" ? data?.records[s.memory_ids[0]] : undefined} by={by} busy={busy} onAct={onAct} />)}
          </ul>
        </section>
      ))}
      <div className="sheet-foot">
        <span className="grow" />
        <button className="btn" onClick={onClose}>{t("common.close")}</button>
      </div>
    </Sheet>
  );
}

/** The line above the entries (or inside one entry) saying how many notes wait, which opens the review. */
export function ReviewBanner({ count, onOpen }: { count: number; onOpen: () => void }) {
  if (count <= 0) return null;
  return (
    <button className="memory-suggest-banner memory-review-banner" data-action="review" onClick={onOpen}>
      <Icon name="bulb" size={16} /> <span>{plural("memory.review.banner", count)}</span>
      <span className="grow" />
      <span className="memory-badge">{count}</span>
    </button>
  );
}
