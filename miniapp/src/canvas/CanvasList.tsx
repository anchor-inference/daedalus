// The canvases page. A canvas belongs to a chat or to a project, never to a global pile: opened from a
// chat the page shows that chat's canvases and its project's, each section with its own New; opened for
// a project, that project's; opened from the navigation, every canvas under the chat or project it
// belongs to. The editor is a chunk of its own, so the list never loads the board just to show cards.

import { lazy, Suspense, useMemo, useState, type ReactElement } from "react";
import { api } from "../api";
import { Icon, type IconName } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor, useRoute } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { OverflowMenu, PageHeader, Segmented, Sheet, Skeleton, useMedia } from "../ui/index";
import { Page } from "../shell";
import { Button, IconButton } from "../ui/primitives";
import { deleteWithUndo, type MenuItem } from "../ui/dialogs";
import { relTimeLong } from "../format";
import { useEvent } from "../events";
import { TEMPLATES, templateDoc, type TemplateId } from "./templates";
import type { Canvas, CanvasItem, CanvasListing, Scope, ScopeRef } from "./model";
import { CanvasRow, download, fileName, pickCanvasFile } from "./parts";
import "./canvas.css";

const CanvasEditor = lazy(() => import("./CanvasEditor").then((module) => ({ default: module.CanvasEditor })));

type Sort = "recent" | "name";

export function CanvasScreen({ toast, selected }: { toast: (message: string) => void; selected: string | null }) {
  const route = useRoute();
  const sessionId = route.query.get("session");
  const projectId = route.query.get("project");
  const listPath = pathFor("canvas", null, { session: sessionId, project: projectId });
  if (selected) {
    return (
      <Suspense fallback={<div className="cv-editor"><div className="cv-loading">{t("common.loading")}</div></div>}>
        <CanvasEditor id={selected} listPath={listPath} toast={toast} />
      </Suspense>
    );
  }
  return <CanvasList toast={toast} sessionId={sessionId} projectId={projectId} />;
}

function scopeKey(kind: Scope, id: string): string {
  return `${kind}:${id}`;
}

function CanvasList({ toast, sessionId, projectId }: { toast: (message: string) => void; sessionId: string | null; projectId: string | null }) {
  const listKey = sessionId ? `/api/canvases?session_id=${encodeURIComponent(sessionId)}` : projectId ? `/api/canvases?project_id=${encodeURIComponent(projectId)}` : "/api/canvases";
  const { data, loading, error, refresh } = useQuery<CanvasListing>(listKey, { pollMs: 30000, staleMs: 0 });
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("recent");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [renaming, setRenaming] = useState<string | null>(null);
  const [creating, setCreating] = useState<ScopeRef | null>(null);
  const [busy, setBusy] = useState(false);
  const phone = useMedia("(max-width: 1023px)");
  const open = (id: string) => navigate(pathFor("canvas", id, { session: sessionId, project: projectId }));

  useEvent(["canvas.changed"], () => refresh(), [refresh]);

  const sections = useMemo(() => {
    const words = query.trim().toLocaleLowerCase();
    const items = (data?.items ?? []).filter((item) => !hidden.has(item.id) && (!words || item.title.toLocaleLowerCase().includes(words)));
    const sorted = sort === "name" ? [...items].sort((a, b) => a.title.localeCompare(b.title)) : [...items].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    const scopes: ScopeRef[] = [...(data?.scopes ?? [])];
    // A canvas whose scope the host did not list still gets a heading rather than vanishing.
    for (const item of sorted) if (!scopes.some((scope) => scopeKey(scope.kind, scope.id) === scopeKey(item.scope, item.scope_id))) scopes.push({ kind: item.scope, id: item.scope_id, name: item.scope_name });
    return scopes.map((scope) => ({ scope, items: sorted.filter((item) => item.scope === scope.kind && item.scope_id === scope.id) }));
  }, [data, query, sort, hidden]);

  const chatScope = data?.scopes.find((scope) => scope.kind === "chat") ?? null;
  const projectScope = data?.scopes.find((scope) => scope.kind === "project") ?? null;
  const total = (data?.items ?? []).filter((item) => !hidden.has(item.id)).length;

  async function create(scope: ScopeRef, template: TemplateId) {
    if (busy) return;
    setBusy(true);
    try {
      const title = template === "blank" ? t("canvas.untitled") : t(`canvas.tpl.${template}`);
      const made = await api.post<Canvas>("/api/canvases", { title, scope: scope.kind, scope_id: scope.id, owner_session_id: sessionId, doc: templateDoc(template) });
      setCreating(null);
      invalidate("/api/canvases");
      open(made.id);
    } catch (exc) { toast(errorText(exc)); } finally { setBusy(false); }
  }

  async function importInto(scope: ScopeRef) {
    const picked = await pickCanvasFile();
    if (!picked) return;
    try {
      const made = await api.post<Canvas>("/api/canvases/import", { title: picked.name || t("canvas.untitled"), scope: scope.kind, scope_id: scope.id, owner_session_id: sessionId, text: picked.text });
      invalidate("/api/canvases");
      open(made.id);
    } catch (exc) { toast(errorText(exc)); }
  }

  async function duplicate(item: CanvasItem) {
    try {
      await api.post<Canvas>(`/api/canvases/${item.id}/duplicate`, { title: t("canvas.copy.title", { title: item.title }).slice(0, 160) });
      invalidate("/api/canvases");
      refresh();
    } catch (exc) { toast(errorText(exc)); }
  }

  async function move(item: CanvasItem, scope: ScopeRef) {
    try {
      await api.post<Canvas>(`/api/canvases/${item.id}/move`, { scope: scope.kind, scope_id: scope.id });
      invalidate("/api/canvases");
      refresh();
      toast(t("canvas.moved", { where: scope.name }));
    } catch (exc) { toast(errorText(exc)); }
  }

  async function exportFile(item: CanvasItem) {
    try {
      const response = await api.fetch(`/api/canvases/${item.id}/export`);
      if (!response.ok) throw new Error(t("canvas.export.failed"));
      download(await response.blob(), `${fileName(item.title)}.canvas`);
    } catch (exc) { toast(errorText(exc)); }
  }

  function remove(item: CanvasItem) {
    setHidden((set) => new Set(set).add(item.id));
    deleteWithUndo(
      t("canvas.deleted", { title: item.title }),
      async () => { await api.delete(`/api/canvases/${item.id}`); invalidate("/api/canvases"); refresh(); },
      () => setHidden((set) => { const next = new Set(set); next.delete(item.id); return next; }),
      (exc) => toast(errorText(exc)),
    );
  }

  async function rename(item: CanvasItem, title: string) {
    setRenaming(null);
    const next = title.trim();
    if (!next || next === item.title) return;
    try {
      await api.patch(`/api/canvases/${item.id}`, { title: next, version: item.version });
      invalidate("/api/canvases");
      refresh();
    } catch (exc) { toast(errorText(exc)); }
  }

  function menuFor(item: CanvasItem): MenuItem[] {
    const other = item.scope === "chat" ? projectScope : chatScope;
    return [
      { label: t("canvas.open"), icon: "external", onSelect: () => open(item.id) },
      { label: t("canvas.rename"), icon: "pen", onSelect: () => setRenaming(item.id) },
      { label: t("canvas.duplicate"), icon: "copy", onSelect: () => void duplicate(item) },
      ...(other ? [{ label: t(other.kind === "project" ? "canvas.move.project" : "canvas.move.chat"), icon: (other.kind === "project" ? "folder" : "chat") as IconName, onSelect: () => void move(item, other) } as MenuItem] : []),
      { label: t("canvas.export"), icon: "download", onSelect: () => void exportFile(item) },
      "-",
      { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => remove(item) },
    ];
  }

  const nothingAnywhere = !sessionId && !projectId && data !== undefined && data.items.length === 0;
  const shown = sections.reduce((n, section) => n + section.items.length, 0);
  const showTools = total > 3;
  // A phone's header has room for its menu and the title; the search goes into the page under it.
  const search = showTools && (
    <label className="cv-search">
      <Icon name="search" size={16} />
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("canvas.search")} aria-label={t("canvas.search")} />
    </label>
  );
  return (
    <>
      <PageHeader
        title={t("nav.canvas")}
        actions={showTools && <>
          {!phone && search}
          <Segmented<Sort> value={sort} onChange={setSort} label={t("canvas.sort")} options={[{ id: "recent", label: t("canvas.sort.recent") }, { id: "name", label: t("canvas.sort.name") }]} />
        </>}
      />
      <Page width="list" className="canvas-screen">
        {phone && search}
        {error && !data && <div className="empty"><b>{t("canvas.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {loading && !data && <Skeleton rows={3} />}
        {nothingAnywhere && (
          <div className="empty">
            <Icon name="grid" size={20} />
            <b>{t("canvas.empty.title")}</b>
            <div>{t("canvas.empty.body")}</div>
          </div>
        )}
        {data && !nothingAnywhere && query && shown === 0 && <div className="empty"><Icon name="search" size={20} /><b>{t("canvas.search.none")}</b></div>}
        {sections.map(({ scope, items }) => {
          const scoped = !!sessionId || !!projectId;
          if (!scoped && !items.length) return null;
          if (query && !items.length) return null;
          return (
            <section key={scopeKey(scope.kind, scope.id)} className="cv-section" aria-label={scope.name}>
              <div className="cv-section-head">
                <h2>
                  <Icon name={scope.kind === "project" ? "folder" : "chat"} size={14} />
                  <span>{scope.kind === "chat" && sessionId === scope.id ? t("canvas.scope.this.chat") : scope.kind === "project" ? t("canvas.scope.project", { name: scope.name }) : scope.name}</span>
                  <span className="cv-section-count">{items.length}</span>
                </h2>
                <IconButton icon="import" size={28} label={t("canvas.import.into", { where: scope.name })} onClick={() => void importInto(scope)} />
                <Button size="sm" icon="plus" disabled={busy} aria-label={t("canvas.new.in", { where: scope.name })} onClick={() => setCreating(scope)}>{t("canvas.new")}</Button>
              </div>
              {items.length === 0 ? (
                <div className="cv-section-empty">{t(scope.kind === "chat" ? "canvas.empty.chat" : "canvas.empty.project")}</div>
              ) : (
                <div className="cv-list" role="list">
                  {items.map((item) => (
                    <CanvasRow key={item.id} item={item} renaming={renaming === item.id} menu={menuFor(item)} onOpen={() => open(item.id)} onRename={(title) => void rename(item, title)} onCancelRename={() => setRenaming(null)} />
                  ))}
                </div>
              )}
            </section>
          );
        })}
        {creating && (
          <Sheet title={t("canvas.new.in", { where: creating.name })} size="md" onClose={() => setCreating(null)}>
            <TemplatePicker onPick={(template) => void create(creating, template)} busy={busy} />
          </Sheet>
        )}
      </Page>
    </>
  );
}

function TemplatePicker({ onPick, busy }: { onPick: (template: TemplateId) => void; busy: boolean }) {
  return (
    <div className="cv-templates" role="list">
      {TEMPLATES.map((template) => (
        <button key={template} role="listitem" className="cv-template" disabled={busy} onClick={() => onPick(template)}>
          <span className="cv-template-art" aria-hidden="true">{TEMPLATE_ART[template]}</span>
          <strong>{t(`canvas.tpl.${template}`)}</strong>
          <small>{t(`canvas.tpl.${template}.hint`)}</small>
        </button>
      ))}
    </div>
  );
}

/** Small line drawings of what each template starts with, in the app's own colours. */
const TEMPLATE_ART: Record<TemplateId, ReactElement> = {
  blank: <svg viewBox="0 0 96 60"><rect x="30" y="18" width="36" height="24" rx="4" strokeDasharray="4 4" /><path d="M48 25v10M43 30h10" /></svg>,
  flowchart: <svg viewBox="0 0 96 60"><rect x="36" y="3" width="24" height="10" rx="3" /><rect x="36" y="22" width="24" height="10" rx="3" /><rect x="36" y="41" width="24" height="10" rx="3" /><rect x="70" y="41" width="22" height="10" rx="3" /><path d="M48 13v9M48 32v9M60 46h10" /></svg>,
  mindmap: <svg viewBox="0 0 96 60"><rect x="33" y="22" width="30" height="14" rx="4" /><rect x="4" y="6" width="22" height="10" rx="3" /><rect x="70" y="6" width="22" height="10" rx="3" /><rect x="4" y="44" width="22" height="10" rx="3" /><rect x="70" y="44" width="22" height="10" rx="3" /><path d="M33 27C28 27 30 11 26 11M63 27c5 0 3-16 7-16M33 31c-5 0-3 18-7 18M63 31c5 0 3 18 7 18" /></svg>,
  lanes: <svg viewBox="0 0 96 60"><rect x="2" y="4" width="92" height="24" rx="3" strokeDasharray="3 3" /><rect x="2" y="32" width="92" height="24" rx="3" strokeDasharray="3 3" /><rect x="12" y="10" width="18" height="12" rx="2" /><rect x="40" y="38" width="18" height="12" rx="2" /><rect x="68" y="10" width="18" height="12" rx="2" /><path d="M30 16c6 0 4 28 10 28M58 44c6 0 4-28 10-28" /></svg>,
};
