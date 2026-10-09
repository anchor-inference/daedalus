// What an imported chat carries in the conversation: the banner that says where it came from, the
// line where the other program's work ends and Daedalus's begins, the block in Details with the way
// back to the source, the corner mark on its sidebar plate, and the start screen's card that offers
// the freshest sessions other programs left on the machine.

import { useState } from "react";
import { api, type SessionList } from "../api";
import { clock, relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { useQuery } from "../store";
import { copyText } from "../ui/components";
import { errorText } from "../ui";
import { ListRow, SectionHeader } from "../ui/phone";
import { openImport } from "./ImportExplorer";
import { HarnessMark } from "./parts";
import { cardDismissed, cardSessions, dismissCard, harnessMeta, originMessages, shortId, shortPath, type ForeignSession, type ImportedOrigin } from "./model";
import { useFreshSessions } from "./fresh";
import "./imports.css";

/** Pull in what the other program wrote since the import, by the cursor the host kept. */
export async function pullNew(sessionId: string, toast: (text: string) => void, after?: () => void): Promise<void> {
  try {
    const answer = await api.post<{ added: number }>(`/api/sessions/${encodeURIComponent(sessionId)}/import/refresh`, {});
    toast(answer.added > 0 ? plural("imp.pulled", answer.added) : t("imp.pulled.none"));
    after?.();
  } catch (error) {
    toast(errorText(error));
  }
}

/** The masked original as a file: fetched with the session's credentials, since a link cannot carry them. */
export async function downloadOriginal(sessionId: string, origin: ImportedOrigin, toast: (text: string) => void): Promise<void> {
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/import/original`, { headers: api.authHeaders() });
    if (!response.ok) throw new Error(t("imp.original.failed", { code: response.status }));
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const named = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)?.[1];
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = named ? decodeURIComponent(named) : origin.original?.name || `${origin.harness}-${origin.id}.jsonl`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (error) {
    toast(errorText(error));
  }
}

/** The first thing in an imported conversation: which program, how much came over, how many secrets
 *  were hidden, and the two ways back to the source. */
export function ImportBanner({ sessionId, origin, toast, onPulled }: { sessionId: string; origin: ImportedOrigin; toast: (text: string) => void; onPulled?: () => void }) {
  const [pulling, setPulling] = useState(false);
  const name = harnessMeta(origin.harness, origin.harness_name).name;
  const pull = async () => {
    setPulling(true);
    await pullNew(sessionId, toast, onPulled);
    setPulling(false);
  };
  return (
    <div className="imp-banner" data-imported={origin.harness} role="note">
      <HarnessMark id={origin.harness} name={origin.harness_name} />
      <span className="imp-banner-text">
        {t("imp.banner.from")} <b>{name}</b>
        <span className="imp-dot" aria-hidden>·</span><span className="mono">{shortId(origin.id)}</span>
        <span className="imp-dot" aria-hidden>·</span>{plural("imp.messages", originMessages(origin))}
        {origin.counts.tool_calls ? <><span className="imp-dot" aria-hidden>·</span>{plural("imp.calls", origin.counts.tool_calls)}</> : null}
        <span className="imp-dot" aria-hidden>·</span><span data-masked={origin.masked}>{t("imp.banner.masked", { n: origin.masked })}</span>
      </span>
      <span className="imp-banner-acts">
        <button type="button" className={`btn small ${origin.live ? "" : "ghost"}`} onClick={() => void pull()} disabled={pulling} data-pull>
          <Icon name="reload" size={13} />{t("imp.pull")}
        </button>
        {origin.original?.stored && (
          <button type="button" className="btn small ghost" onClick={() => void downloadOriginal(sessionId, origin, toast)} data-original>
            <Icon name="download" size={13} />{t("imp.original")}
          </button>
        )}
      </span>
    </div>
  );
}

/** Where the other program's work ends and Daedalus's begins. */
export function HandoffLine({ at }: { at?: number }) {
  return (
    <div className="imp-handoff" role="separator" data-handoff>
      <Icon name="logo" size={13} />
      <span>{t("imp.handoff")}{at ? ` · ${clock(at)}` : ""}</span>
    </div>
  );
}

/** Details' "Origin": the program, the session there, the model it used, what came over, and the way back. */
export function OriginSection({ sessionId, origin, toast, onPulled }: { sessionId: string; origin: ImportedOrigin; toast: (text: string) => void; onPulled?: () => void }) {
  const name = harnessMeta(origin.harness, origin.harness_name).name;
  const counts = origin.counts;
  const messages = originMessages(origin);
  const brought = origin.mode === "tail" ? t("imp.origin.tail", { all: messages }) : t("imp.origin.full", { all: messages });
  const copy = async () => toast((await copyText(origin.id)) ? t("common.copied") : origin.id);
  return (
    <div className="imp-origin" data-origin={origin.harness}>
      <div className="dt-row"><span className="dt-key">{t("imp.origin.program")}</span><span className="grow">{name}</span></div>
      <div className="dt-row"><span className="dt-key">{t("imp.origin.session")}</span><span className="grow mono truncate" title={origin.id}>{origin.id}</span><button type="button" className="btn small" onClick={() => void copy()}>{t("common.copy")}</button></div>
      {origin.source_model && <div className="dt-row"><span className="dt-key">{t("imp.origin.model")}</span><span className="grow">{origin.source_model}</span></div>}
      {origin.branch && <div className="dt-row"><span className="dt-key">{t("imp.origin.branch")}</span><span className="grow mono">{origin.branch}</span></div>}
      <div className="dt-row"><span className="dt-key">{t("imp.origin.brought")}</span><span className="grow">{brought}</span></div>
      {!!counts.sidechains && <div className="dt-row"><span className="dt-key">{t("imp.origin.subagents")}</span><span className="grow">{t("imp.origin.subagents.n", { n: counts.sidechains })}</span></div>}
      <div className="dt-row"><span className="dt-key">{t("imp.origin.masked")}</span><span className="grow">{origin.masked}</span></div>
      <div className="dt-row sub"><span className="dt-key">{t("imp.origin.when")}</span><span className="grow">{relTime(origin.refreshed_at || origin.imported_at)}</span></div>
      <p className="sub">{origin.live ? t("imp.origin.live", { name }) : t("imp.origin.fork", { name })}</p>
      <div className="btnrow">
        <button type="button" className="btn small" onClick={() => void pullNew(sessionId, toast, onPulled)}><Icon name="reload" size={14} />{t("imp.pull")}</button>
        {origin.original?.stored && <button type="button" className="btn small" onClick={() => void downloadOriginal(sessionId, origin, toast)}><Icon name="download" size={14} />{t("imp.original")}</button>}
      </div>
      {origin.original && !origin.original.stored && origin.original.reason && <p className="sub" data-original-missing>{t("imp.origin.nooriginal", { reason: origin.original.reason })}</p>}
    </div>
  );
}

/** The program a sidebar row's chat came from, as its plate's corner mark. */
export function importedHarness(row: { imported_from?: string | null }): string {
  return typeof row.imported_from === "string" ? row.imported_from : "";
}

export function PlateMark({ harness }: { harness: string }) {
  const meta = harnessMeta(harness);
  return <span className="imp-hmini" style={{ ["--c" as string]: meta.color }} title={t("imp.mark.title", { name: meta.name })} aria-label={t("imp.mark.title", { name: meta.name })}>{meta.mark.charAt(0)}</span>;
}

function destinationOf(s: ForeignSession): string {
  if (s.project && s.project.kind === "project") return t("imp.card.into", { name: s.project.name });
  return t("imp.card.new");
}

/** The fresh sessions the machine holds, for the start screen; nothing while the machine does not answer. */
function useFresh(): { sessions: ForeignSession[]; home?: string; dismiss: () => void } {
  const [dismissed, setDismissed] = useState(cardDismissed);
  const fresh = useFreshSessions(!dismissed);
  const listing = useQuery<SessionList>(dismissed ? null : "/api/sessions?view=all", { staleMs: 30000 });
  const last = Object.fromEntries((listing.data?.projects ?? []).map((p) => [p.id, p.last_message_at]));
  return { sessions: dismissed ? [] : cardSessions(fresh.sessions, last), home: fresh.home, dismiss: () => { dismissCard(); setDismissed(true); } };
}

/** "Continue from other programs" on a desktop's start screen: up to three, closed for good by its ×. */
export function StartImportCard() {
  const { sessions, home, dismiss } = useFresh();
  if (!sessions.length) return null;
  return (
    <section className="imp-card start-import" aria-label={t("imp.card")}>
      <div className="si-head">
        <span>{t("imp.card")}</span>
        <span className="num">{sessions.length}</span>
        <span className="grow" />
        <button type="button" className="si-more" onClick={() => openImport()}>{t("imp.card.all")}</button>
        <button type="button" className="iconbtn small quiet" onClick={dismiss} aria-label={t("imp.card.hide")} title={t("imp.card.hide")}><Icon name="close" size={14} /></button>
      </div>
      {sessions.map((s) => (
        <div key={`${s.harness}:${s.id}`} className="si-row" data-fresh={s.id} role="link" tabIndex={0}
          onClick={() => openImport({ harness: s.harness, path: s.cwd, session: s.id })}
          onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openImport({ harness: s.harness, path: s.cwd, session: s.id }); } }}>
          <HarnessMark id={s.harness} size="sm" />
          <span className="si-main">
            <span className="si-title truncate">{s.title}</span>
            <span className="si-meta">
              <span>{harnessMeta(s.harness).name}</span><span className="sep">·</span>
              <span className="mono truncate">{shortPath(s.cwd, home)}</span><span className="sep">·</span>
              <span className="truncate">{destinationOf(s)}</span>
            </span>
          </span>
          <span className="si-time num">{relTime(s.updated_at)}</span>
          <button type="button" className="btn small" onClick={(e) => { e.stopPropagation(); openImport({ harness: s.harness, path: s.cwd, session: s.id }); }}>{t("imp.card.continue")}</button>
        </div>
      ))}
    </section>
  );
}

/** The same card on a phone's home, as list rows. */
export function PhoneImportCard() {
  const { sessions, home, dismiss } = useFresh();
  if (!sessions.length) return null;
  return (
    <>
      <SectionHeader count={sessions.length} action={<button type="button" className="ph-link" onClick={dismiss}>{t("imp.card.hide.short")}</button>}>{t("imp.card")}</SectionHeader>
      <div className="ph-list imp-card">
        {sessions.map((s) => (
          <ListRow key={`${s.harness}:${s.id}`} data={{ fresh: s.id }} title={s.title} onOpen={() => openImport({ harness: s.harness, path: s.cwd, session: s.id })}
            lead={<HarnessMark id={s.harness} size="md" />}
            meta={<span className="truncate">{harnessMeta(s.harness).name} · {shortPath(s.cwd, home)}</span>}
            trail={relTime(s.updated_at)} />
        ))}
      </div>
    </>
  );
}
