// The diagrams page: a grid of cards with real thumbnails, search and sort, rename, duplicate and
// delete with undo, and the diagrams of one chat when it is opened from that chat. The editor is a
// chunk of its own, so the list never loads Excalidraw just to show what there is.

import { lazy, Suspense, useMemo, useRef, useState, type ReactElement } from "react";
import { api } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor, useRoute } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { OverflowMenu, PageHeader, Segmented, Sheet } from "../ui/index";
import { deleteWithUndo } from "../ui/dialogs";
import { relTimeLong } from "../format";
import { TEMPLATES, templateSkeleton, type TemplateId } from "../diagram-templates";
import { DiagramThumb, type Diagram, type DiagramItem } from "./diagramparts";
import "./diagrams.css";

const DiagramEditor = lazy(() => import("./DiagramEditor").then((module) => ({ default: module.DiagramEditor })));

type Sort = "recent" | "name";

/** Build a template's scene with Excalidraw's own converter, loaded only when a template is chosen. */
async function templateScene(id: TemplateId) {
  const skeleton = templateSkeleton(id);
  if (!skeleton.length) return { elements: [], appState: {}, files: {} };
  const { convertToExcalidrawElements } = await import("@excalidraw/excalidraw");
  return { elements: convertToExcalidrawElements(skeleton as any, { regenerateIds: false }), appState: {}, files: {} };
}

export function DiagramsScreen({ toast, selected }: { toast: (message: string) => void; selected: string | null }) {
  const route = useRoute();
  const sessionId = route.query.get("session");
  if (selected) {
    return (
      <Suspense fallback={<div className="diagram-editor"><div className="diagram-loading">{t("common.loading")}</div></div>}>
        <DiagramEditor id={selected} sessionId={sessionId} toast={toast} />
      </Suspense>
    );
  }
  return <DiagramList toast={toast} sessionId={sessionId} />;
}

function DiagramList({ toast, sessionId }: { toast: (message: string) => void; sessionId: string | null }) {
  const listKey = sessionId ? `/api/diagrams?session_id=${encodeURIComponent(sessionId)}` : "/api/diagrams";
  const { data: items, loading, error, refresh } = useQuery<DiagramItem[]>(listKey, { pollMs: 10000, staleMs: 0 });
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("recent");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [renaming, setRenaming] = useState<string | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [busy, setBusy] = useState(false);

  const shown = useMemo(() => {
    const words = query.trim().toLocaleLowerCase();
    const list = (items ?? []).filter((item) => !hidden.has(item.id) && (!words || item.title.toLocaleLowerCase().includes(words)));
    return sort === "name" ? [...list].sort((a, b) => a.title.localeCompare(b.title)) : list;
  }, [items, query, sort, hidden]);

  async function create(template: TemplateId) {
    if (busy) return;
    setBusy(true);
    try {
      const title = template === "blank" ? t("diagrams.untitled") : t(`diagrams.tpl.${template}`);
      const made = await api.post<Diagram>("/api/diagrams", { title, scene: await templateScene(template) });
      setChoosing(false);
      invalidate("/api/diagrams");
      navigate(pathFor("diagrams", made.id, { session: sessionId }));
    } catch (exc) { toast(errorText(exc)); } finally { setBusy(false); }
  }

  async function duplicate(item: DiagramItem) {
    try {
      await api.post<Diagram>(`/api/diagrams/${item.id}/duplicate`, { title: t("diagrams.copy.title", { title: item.title }).slice(0, 160) });
      toast(t("diagrams.duplicated"));
      invalidate("/api/diagrams");
      refresh();
    } catch (exc) { toast(errorText(exc)); }
  }

  function remove(item: DiagramItem) {
    setHidden((set) => new Set(set).add(item.id));
    deleteWithUndo(
      t("diagrams.deleted", { title: item.title }),
      async () => { await api.delete(`/api/diagrams/${item.id}`); invalidate("/api/diagrams"); refresh(); },
      () => setHidden((set) => { const next = new Set(set); next.delete(item.id); return next; }),
      (exc) => toast(errorText(exc)),
    );
  }

  async function rename(item: DiagramItem, title: string) {
    setRenaming(null);
    const next = title.trim();
    if (!next || next === item.title) return;
    try {
      await api.patch(`/api/diagrams/${item.id}`, { title: next, version: item.version });
      invalidate("/api/diagrams");
      refresh();
    } catch (exc) { toast(errorText(exc)); }
  }

  const empty = items !== undefined && items.length === 0;
  return (
    <div className="screen wide diagrams-screen">
      <PageHeader
        title={t("nav.diagrams")}
        actions={<button className="btn primary" onClick={() => setChoosing(true)} disabled={busy}><Icon name="plus" size={16} /> {t("diagrams.new")}</button>}
      />
      {sessionId && (
        <div className="diagram-scope">
          <span className="chip on"><Icon name="bots" size={14} />{t("diagrams.scope.chat")}</span>
          <button className="linkbtn" onClick={() => navigate(pathFor("diagrams"), { replace: true })}>{t("diagrams.scope.all")}</button>
        </div>
      )}
      {!empty && (
        <div className="diagram-toolbar">
          <label className="diagram-search">
            <Icon name="search" size={15} />
            <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("diagrams.search")} aria-label={t("diagrams.search")} />
          </label>
          <Segmented<Sort> value={sort} onChange={setSort} label={t("diagrams.sort")} options={[{ id: "recent", label: t("diagrams.sort.recent") }, { id: "name", label: t("diagrams.sort.name") }]} />
        </div>
      )}
      {error && !items && <div className="empty">{error}</div>}
      {loading && !items && <div className="diagram-grid">{[0, 1, 2].map((key) => <div key={key} className="diagram-card skeleton" aria-hidden="true" />)}</div>}
      {empty && (
        <div className="diagram-empty">
          <div className="diagram-empty-mark"><Icon name="pen" size={26} /></div>
          <h2>{sessionId ? t("diagrams.empty.chat") : t("diagrams.empty.title")}</h2>
          <p>{t("diagrams.empty.body")}</p>
          <TemplatePicker onPick={(template) => void create(template)} busy={busy} />
        </div>
      )}
      {!empty && items && shown.length === 0 && <div className="empty">{t("diagrams.search.none")}</div>}
      {!empty && shown.length > 0 && (
        <div className="diagram-grid">
          {shown.map((item) => (
            <DiagramCard
              key={item.id}
              item={item}
              renaming={renaming === item.id}
              onOpen={() => navigate(pathFor("diagrams", item.id, { session: sessionId }))}
              onRename={(title) => void rename(item, title)}
              onCancelRename={() => setRenaming(null)}
              menu={[
                { label: t("diagrams.open"), icon: "external", onSelect: () => navigate(pathFor("diagrams", item.id, { session: sessionId })) },
                { label: t("diagrams.rename"), icon: "pen", onSelect: () => setRenaming(item.id) },
                { label: t("diagrams.duplicate"), icon: "copy", onSelect: () => void duplicate(item) },
                "-",
                { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => remove(item) },
              ]}
            />
          ))}
        </div>
      )}
      {choosing && (
        <Sheet title={t("diagrams.new")} onClose={() => setChoosing(false)}>
          <TemplatePicker onPick={(template) => void create(template)} busy={busy} />
        </Sheet>
      )}
    </div>
  );
}

function DiagramCard({ item, renaming, menu, onOpen, onRename, onCancelRename }: { item: DiagramItem; renaming: boolean; menu: Parameters<typeof OverflowMenu>[0]["items"]; onOpen: () => void; onRename: (title: string) => void; onCancelRename: () => void }) {
  const field = useRef<HTMLInputElement>(null);
  return (
    <article className="diagram-card">
      <button className="diagram-card-open" onClick={onOpen} aria-label={item.title} tabIndex={renaming ? -1 : 0}>
        <DiagramThumb path={`/api/diagrams/${item.id}/preview`} version={item.version} />
      </button>
      <div className="diagram-card-foot">
        <div className="diagram-card-text">
          {renaming ? (
            <input
              ref={field}
              className="field diagram-rename"
              autoFocus
              defaultValue={item.title}
              maxLength={160}
              aria-label={t("diagrams.title")}
              onFocus={(event) => event.target.select()}
              onKeyDown={(event) => {
                if (event.key === "Enter") onRename(event.currentTarget.value);
                if (event.key === "Escape") { event.stopPropagation(); onCancelRename(); }
              }}
              onBlur={(event) => onRename(event.currentTarget.value)}
            />
          ) : (
            <button className="diagram-card-title" onClick={onOpen}>{item.title}</button>
          )}
          <span className="diagram-card-meta">
            {item.updated_by === "agent" && <Icon name="bots" size={12} />}
            <time dateTime={item.updated_at}>{t(item.updated_by === "agent" ? "diagrams.meta.agent" : "diagrams.meta.you", { time: relTimeLong(item.updated_at) })}</time>
            {item.shared && <span className="diagram-card-shared" title={t("diagrams.share.on")} aria-label={t("diagrams.share.on")}><Icon name="link" size={12} /></span>}
          </span>
        </div>
        <OverflowMenu small items={menu} label={t("diagrams.actions", { title: item.title })} />
      </div>
    </article>
  );
}

function TemplatePicker({ onPick, busy }: { onPick: (template: TemplateId) => void; busy: boolean }) {
  return (
    <div className="diagram-templates" role="list">
      {TEMPLATES.map((template) => (
        <button key={template} role="listitem" className="diagram-template" disabled={busy} onClick={() => onPick(template)}>
          <span className={`diagram-template-art ${template}`} aria-hidden="true">{TEMPLATE_ART[template]}</span>
          <strong>{t(`diagrams.tpl.${template}`)}</strong>
          <small>{t(`diagrams.tpl.${template}.hint`)}</small>
        </button>
      ))}
    </div>
  );
}

/** Small line drawings of what each template starts with, in the app's own colours. */
const TEMPLATE_ART: Record<TemplateId, ReactElement> = {
  blank: <svg viewBox="0 0 96 60"><rect x="30" y="18" width="36" height="24" rx="4" strokeDasharray="4 4" /><path d="M48 25v10M43 30h10" /></svg>,
  flowchart: <svg viewBox="0 0 96 60"><ellipse cx="48" cy="9" rx="14" ry="6" /><rect x="34" y="22" width="28" height="12" rx="3" /><path d="M48 15v7M48 34v6" /><path d="M48 40l9 7-9 7-9-7z" /></svg>,
  mindmap: <svg viewBox="0 0 96 60"><ellipse cx="48" cy="30" rx="15" ry="9" /><rect x="4" y="6" width="22" height="10" rx="3" /><rect x="70" y="6" width="22" height="10" rx="3" /><rect x="4" y="44" width="22" height="10" rx="3" /><rect x="70" y="44" width="22" height="10" rx="3" /><path d="M35 25L26 15M61 25l9-10M35 35l-9 10M61 35l9 10" /></svg>,
  swimlane: <svg viewBox="0 0 96 60"><rect x="2" y="4" width="92" height="24" rx="2" strokeDasharray="3 3" /><rect x="2" y="32" width="92" height="24" rx="2" strokeDasharray="3 3" /><rect x="12" y="10" width="18" height="12" rx="2" /><rect x="40" y="38" width="18" height="12" rx="2" /><rect x="68" y="10" width="18" height="12" rx="2" /><path d="M30 16l10 22M58 44l10-22" /></svg>,
};
