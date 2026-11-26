// The operator's secrets in the app: one manager, drawn as a wide sheet over a chat (opened from the
// composer's "+") and as the main block of Settings → Security, and the chip a message carries.
//
// A value never travels with a list. The host lists names, scopes and times only; a value is fetched
// for one secret at a time, on the reveal route, when the operator presses its eye or Copy, and the
// one revealed value is dropped again when the selection changes, the pane or sheet closes, the tab
// is hidden, or half a minute passes. A value being typed lives in an uncontrolled field: React writes
// a controlled textarea's text into its `defaultValue`, which is the element's markup, so a typed
// password would have been in the page's serialized HTML for as long as the field held it.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { api, type SessionList } from "./api";
import { absTime, relTime, relTimeLong } from "./format";
import { num, plural, t } from "./i18n";
import { Icon } from "./icons";
import { navigate, pathFor } from "./router";
import { useQuery } from "./store";
import { errorText } from "./ui";
import { Sheet, toast } from "./ui/dialogs";
import { timeAgo, useCopied } from "./ui/components";
import { Button, Field, IconButton, Kbd, Segmented, Select, confirmDialog } from "./ui/primitives";
import { Row, SetGroup } from "./settingsrow";
import {
  groupByProject, inTab, matchesSecret, placeholderOf, secretName, sortSecrets, usedBy, validSecretName,
  type AttachedSecret, type SavedLoginView, type SecretAudit, type SecretScope, type SecretSort, type SecretTab, type SecretUsage, type SecretView, type SecretsList,
} from "./secretsmodel";

export { matchesSecret, placeholderOf, secretName, validSecretName } from "./secretsmodel";
export type { AttachedSecret, SavedLoginView, SecretScope, SecretView, SecretsList } from "./secretsmodel";

/** How long a revealed value stays on screen before it hides by itself. */
export const REVEAL_MS = 30_000;
/** How long a removed secret can be brought back before the request goes out. */
export const UNDO_MS = 6000;
/** The settings column is wide enough for the list and the open secret side by side from here; below
 *  it the open secret is a sheet over the list. */
export const SPLIT_MIN = 900;
/** The dots a hidden value is drawn as: always as many, so nothing on screen says how long it is. */
const MASK = "••••••••••••";
/** What the attributes of a field that takes a secret tell the browser's and the password managers'
 *  autofill: not a login, nothing to fill, nothing to offer to save. */
const NO_AUTOFILL = { autoComplete: "off", autoCapitalize: "none", autoCorrect: "off", spellCheck: false, "data-1p-ignore": "", "data-lpignore": "true", "data-bwignore": "true", "data-form-type": "other" } as const;

type Selected = string | "new" | null;
type Layout = "split" | "sheet" | "push";

/** The host's audit words in the reader's language; one the app has no word for is shown as written. */
function auditAction(action: string): string {
  const words: Record<string, () => string> = {
    create: () => t("secret.audit.create"), change: () => t("secret.audit.change"), rename: () => t("secret.audit.rename"), note: () => t("secret.audit.note"),
    reveal: () => t("secret.audit.reveal"), copy: () => t("secret.audit.copy"), delete: () => t("secret.audit.delete"),
  };
  return words[action]?.() ?? action;
}

function whereLabel(secret: SecretView): string {
  return t(secret.scope === "project" ? "secret.list.project" : "secret.list.session", { title: secret.scope_title || secret.scope_id });
}

/** "Router setup" with the mark of a chat or a project: where a secret belongs, as a row and the pane say it. */
function Where({ secret }: { secret: SecretView }) {
  return (
    <span className="secret-where" data-scope={secret.scope} title={whereLabel(secret)}>
      <Icon name={secret.scope === "project" ? "folder" : "chat"} size={12} />
      <span>{secret.scope_title || secret.scope_id}</span>
    </span>
  );
}

/** A field that takes a value: several lines, because a key or a certificate has them, in the
 *  monospace face, masked until its eye is pressed. Uncontrolled, for the reason at the top of the
 *  file; the parent reads it through `field` and hears only whether it holds anything. */
function ValueField({ id, label, field, onFilled, placeholder, hint }: { id: string; label: string; field: MutableRefObject<HTMLTextAreaElement | null>; onFilled: (filled: boolean) => void; placeholder?: string; hint?: ReactNode }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="field-box">
      <label className="field-label" htmlFor={id}>{label}</label>
      <div className="secret-value-row">
        <textarea ref={field} id={id} className={`field secret-value ${shown ? "shown" : ""}`} rows={6} placeholder={placeholder} {...NO_AUTOFILL}
          onInput={(e) => onFilled(e.currentTarget.value.trim().length > 0)} aria-describedby={hint ? `${id}-note` : undefined} />
        <IconButton className="flat secret-eye" icon={shown ? "eyeoff" : "eye"} label={t(shown ? "secret.value.hide" : "secret.value.show")} pressed={shown} onClick={() => setShown((s) => !s)} />
      </div>
      {hint && <div id={`${id}-note`} className="sub field-hint">{hint}</div>}
    </div>
  );
}

/** The manager. With a chat (`sessionId`) it is the body of the composer's sheet: the chat's own, its
 *  project's and every other secret in three views, each of the first two attachable to the message
 *  being written. Without one it is Settings → Security's list of every secret. */
export function SecretsManager({ sessionId, phone, attached = [], onAttach, onDetach, onChanged, onFetched, guard, requestNew, onLeave }: {
  sessionId?: string;
  phone: boolean;
  attached?: string[];
  /** `done`: the secret was just made with "Save and attach", and the sheet has done its job. */
  onAttach?: (secret: AttachedSecret, done: boolean) => void;
  onDetach?: (id: string) => void;
  /** A secret was renamed (its new name) or removed (null): a draft holding it follows. */
  onChanged?: (id: string, name: string | null) => void;
  onFetched?: (list: SecretsList) => void;
  /** Filled with the question asked before the manager goes with edits unsaved; the sheet asks it on close. */
  guard?: MutableRefObject<(() => Promise<boolean>) | null>;
  /** Bumped by Settings' header button: open the new-secret form. */
  requestNew?: number;
  /** Following a link to a chat: the sheet closes first. */
  onLeave?: (href: string) => void;
}) {
  const chat = !!sessionId;
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const listBox = useRef<HTMLDivElement>(null);
  const width = useWidth(root, !chat);
  const layout: Layout = chat ? (phone ? "push" : "split") : !phone && width >= SPLIT_MIN ? "split" : "sheet";

  const [list, setList] = useState<SecretsList | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SecretSort>("used");
  const [tab, setTab] = useState<SecretTab>("chat");
  const [selected, setSelected] = useState<Selected>(null);
  const [revealed, setRevealed] = useState<{ id: string; value: string } | null>(null);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const hideTimer = useRef(0);
  const removals = useRef(new Map<string, number>());
  const dirty = useRef(false);
  const focused = useRef(false);
  const { data: sessions } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });

  const load = useCallback(() => {
    const path = sessionId ? `/api/secrets?session_id=${encodeURIComponent(sessionId)}` : "/api/secrets";
    return api.get<SecretsList>(path)
      .then((got) => { setList(got); onFetched?.(got); return got; })
      .catch((e) => { setList({ secrets: [] }); toast(errorText(e)); return null; });
  }, [sessionId, onFetched]);

  const all = useMemo(() => [...(list?.secrets ?? []), ...(list?.others ?? [])].filter((s) => !gone.has(s.id)), [list, gone]);
  // The host leaves a secret whose value does not open out of a chat's own list, so whether one can be
  // attached is read from that list, not from its scope.
  const attachable = useMemo(() => new Set((list?.secrets ?? []).map((s) => s.id)), [list]);
  const projectId = chat ? list?.project_id ?? null : null;

  // The first view: the chat's own when it has any, then its project's, then everything.
  useEffect(() => {
    let live = true;
    void load().then((got) => {
      if (!live || !got) return;
      const every = [...got.secrets, ...(got.others ?? [])];
      const first = chat ? (["chat", "project", "all"] as SecretTab[]).find((v) => every.some((s) => inTab(s, v, sessionId, got.project_id))) ?? "chat" : "all";
      setTab(first);
      setSelected((cur) => {
        if (cur !== null) return cur;
        // A chat with no secret at all opens on the form: open, type, attach is still the quickest way.
        if (chat && every.length === 0) return "new";
        if (chat && !phone) return sortSecrets(every.filter((s) => inTab(s, first, sessionId, got.project_id)), "used")[0]?.id ?? null;
        return null;
      });
    });
    return () => { live = false; };
    // The first answer only: later loads keep the view the operator chose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  const hide = useCallback(() => {
    window.clearTimeout(hideTimer.current);
    setRevealed(null);
  }, []);

  // Leaving takes the revealed value out of memory with it, and sends the removals still waiting for
  // their undo: closing the sheet is not taking the removal back.
  useEffect(() => {
    const pending = removals.current;
    return () => {
      window.clearTimeout(hideTimer.current);
      pending.forEach((timer, id) => {
        window.clearTimeout(timer);
        void api.delete(`/api/secrets/${id}`).catch(() => {});
      });
      pending.clear();
    };
  }, []);

  // A tab put away is a screen nobody is watching: what was revealed on it goes.
  useEffect(() => {
    const away = () => { if (document.visibilityState === "hidden") hide(); };
    document.addEventListener("visibilitychange", away);
    return () => document.removeEventListener("visibilitychange", away);
  }, [hide]);

  const discard = useCallback(async () => {
    if (!dirty.current) return true;
    const sure = await confirmDialog({ title: t("secret.discard.title"), body: t("secret.discard.body"), action: t("secret.discard"), cancel: t("secret.discard.keep"), danger: true });
    if (sure) dirty.current = false;
    return sure;
  }, []);
  useEffect(() => {
    if (!guard) return;
    guard.current = discard;
    return () => { guard.current = null; };
  }, [guard, discard]);

  const choose = useCallback(async (next: Selected) => {
    if (next === selected) return;
    if (!(await discard())) return;
    hide();
    dirty.current = false;
    setSelected(next);
  }, [selected, discard, hide]);

  useEffect(() => {
    if (requestNew) void choose("new");
    // Only a new press of the header's button opens the form.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestNew]);

  async function reveal(secret: SecretView) {
    if (revealed?.id === secret.id) return hide();
    try {
      const { value } = await api.post<{ value: string }>(`/api/secrets/${secret.id}/reveal`, { purpose: "reveal" });
      window.clearTimeout(hideTimer.current);
      setRevealed({ id: secret.id, value });
      hideTimer.current = window.setTimeout(() => setRevealed(null), REVEAL_MS);
    } catch (e) {
      toast(errorText(e));
    }
  }

  const [copied, copyValue] = useCopied();
  async function copy(secret: SecretView) {
    try {
      const { value } = await api.post<{ value: string }>(`/api/secrets/${secret.id}/reveal`, { purpose: "copy" });
      if (await copyValue(value, secret.id)) toast(t("secret.copied", { name: secret.name }));
      else toast(t("secret.row.copy.failed"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function askRemove(secret: SecretView) {
    // Not knowing where it is named is no reason to refuse removing it; the warning is a courtesy.
    const usage = await api.get<SecretUsage>(`/api/secrets/${secret.id}/usage`).catch((): SecretUsage => ({ conversations: 0, titles: [] }));
    const onDraft = attached.includes(secret.name) && attachable.has(secret.id);
    const sure = await confirmDialog({
      title: t("secret.remove.title", { name: secret.name }),
      body: (
        <>
          <p className="secret-warn-where">{t(secret.scope === "project" ? "secret.remove.where.project" : "secret.remove.where.session", { title: secret.scope_title || secret.scope_id })}</p>
          {onDraft && <p className="secret-warn">{t("secret.remove.draft")}</p>}
          {usage.conversations > 0 && <p className="secret-warn">{t("secret.remove.used", { n: String(usage.conversations), titles: usage.titles.map((c) => c.title || c.id).join(", ") })}</p>}
          {t("secret.remove.body")}
        </>
      ),
      action: t("common.remove"),
      danger: true,
    });
    if (sure) remove(secret);
  }

  function remove(secret: SecretView) {
    hide();
    dirty.current = false;
    // The open pane moves to the next row rather than to nothing, as a mail client's does.
    const at = visible.findIndex((s) => s.id === secret.id);
    const next = visible[at + 1] ?? visible[at - 1];
    if (selected === secret.id) setSelected(layout === "split" && next ? next.id : null);
    setGone((cur) => new Set(cur).add(secret.id));
    onChanged?.(secret.id, null);
    const timer = window.setTimeout(() => {
      removals.current.delete(secret.id);
      api.delete(`/api/secrets/${secret.id}`)
        .then(() => setList((cur) => cur && { ...cur, secrets: cur.secrets.filter((s) => s.id !== secret.id), others: cur.others?.filter((s) => s.id !== secret.id) }))
        .catch((e) => {
          setGone((cur) => { const rest = new Set(cur); rest.delete(secret.id); return rest; });
          toast(errorText(e));
        });
    }, UNDO_MS);
    removals.current.set(secret.id, timer);
    toast(t("secret.removed", { name: secret.name }), {
      ms: UNDO_MS,
      undo: () => {
        window.clearTimeout(timer);
        removals.current.delete(secret.id);
        // A chip the removal took off the draft is not put back: attaching it again is one press away.
        setGone((cur) => { const rest = new Set(cur); rest.delete(secret.id); return rest; });
      },
    });
  }

  function saved(secret: SecretView, previous: SecretView) {
    hide();
    dirty.current = false;
    const merge = (items?: SecretView[]) => items?.map((s) => (s.id === secret.id ? { ...s, ...secret, scope_title: s.scope_title } : s));
    setList((cur) => cur && { ...cur, secrets: merge(cur.secrets) ?? [], others: merge(cur.others) });
    if (secret.name !== previous.name) onChanged?.(secret.id, secret.name);
  }

  async function kept(secret: SecretView, attach: boolean) {
    dirty.current = false;
    if (attach && onAttach) {
      onAttach({ id: secret.id, name: secret.name, scope: secret.scope, fresh: true }, true);
      return;
    }
    await load();
    if (!inTab(secret, tab, sessionId, projectId)) setTab(chat ? (secret.scope === "project" ? "project" : "chat") : "all");
    setQuery("");
    setSelected(secret.id);
  }

  const toggle = (secret: SecretView) => {
    if (!attachable.has(secret.id)) return;
    if (attached.includes(secret.name)) onDetach?.(secret.id);
    else onAttach?.({ id: secret.id, name: secret.name, scope: secret.scope, fresh: false }, false);
  };

  const counts = useMemo(() => {
    const found = all.filter((s) => matchesSecret(s, query));
    return { chat: found.filter((s) => inTab(s, "chat", sessionId, projectId)).length, project: found.filter((s) => inTab(s, "project", sessionId, projectId)).length, all: found.length };
  }, [all, query, sessionId, projectId]);
  const shownTab: SecretTab = chat ? tab : "all";
  const visibleUnsorted = all.filter((s) => inTab(s, shownTab, sessionId, projectId) && matchesSecret(s, query));
  const grouped = shownTab === "all";
  const groups = grouped ? groupByProject(visibleUnsorted, sessions, { session: sessionId, project: projectId, name: list?.project_name }).map((g) => ({ ...g, secrets: sortSecrets(g.secrets, sort) })) : null;
  const visible = groups ? groups.flatMap((g) => g.secrets) : sortSecrets(visibleUnsorted, sort);
  const open = selected && selected !== "new" ? all.find((s) => s.id === selected) ?? null : null;

  // Settings measures its column after the first answer: once it has room beside the list, the first
  // secret is open there as it is over a chat, rather than an empty half of the card.
  useEffect(() => {
    if (chat || layout !== "split" || selected !== null || list === null) return;
    if (visible[0]) setSelected(visible[0].id);
    // The first row of the moment the room appeared; a later choice of nothing is the reader's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat, layout, list]);

  // Over a chat on a desktop the keyboard starts in the list, on the open row, so ↑ ↓ and Enter work at once.
  useLayoutEffect(() => {
    if (focused.current || !chat || phone || list === null) return;
    focused.current = true;
    const row = root.current?.querySelector<HTMLButtonElement>(".secret-row.on .secret-row-open") ?? root.current?.querySelector<HTMLButtonElement>(".secret-row-open");
    if (row) row.focus({ preventScroll: true });
    else root.current?.querySelector<HTMLElement>("#secret-name")?.focus();
  });

  // `/` searches and N starts a new secret, from anywhere in the manager but a field. Read in the
  // capture phase: the sheet stops a key's way out of itself, and Settings has a `/` of its own for
  // its search, which a key meant for this list must not also reach.
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const scope = chat ? root.current?.closest(".sheet") : root.current;
      if (!target || !scope || !scope.contains(target)) return;
      if (target.closest("input, textarea, select, [contenteditable='true'], [role='listbox'], [role='menu']")) return;
      if (e.key === "/") {
        e.preventDefault();
        e.stopPropagation();
        search.current?.focus();
      } else if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        e.stopPropagation();
        void choose("new");
      }
    }
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, [chat, choose]);

  function walk(e: React.KeyboardEvent<HTMLElement>) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const rows = [...(listBox.current?.querySelectorAll<HTMLButtonElement>(".secret-row-open") ?? [])];
    if (!rows.length) return;
    e.preventDefault();
    const at = rows.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === "Home" ? 0 : e.key === "End" ? rows.length - 1 : at < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, at + (e.key === "ArrowDown" ? 1 : -1)));
    rows[next].focus();
    rows[next].scrollIntoView?.({ block: "nearest" });
  }

  const leave = (href: string) => {
    if (onLeave) onLeave(href);
    else void discard().then((ok) => { if (ok) navigate(href); });
  };
  const chatTitle = (id: string) => sessions?.sessions.find((s) => s.id === id)?.title || (id === sessionId ? t("secret.this.chat") : id.slice(0, 12));

  const used = (secret: SecretView) => secret.last_used_at ? `${relTime(secret.last_used_at)} · ${num(secret.uses)}×` : t("secret.list.unused");
  const row = (secret: SecretView) => {
    const on = secret.id === selected;
    const canAttach = chat && attachable.has(secret.id);
    const isAttached = canAttach && attached.includes(secret.name);
    return (
      <div key={secret.id} className={`secret-row ${on ? "on" : ""}`} data-secret={secret.name} data-scope={secret.scope} role="listitem">
        <button type="button" className="secret-row-open" aria-current={on || undefined} onClick={() => void choose(secret.id)}>
          <span className="secret-row-name">{secret.name}</span>
          <Where secret={secret} />
          <span className={`secret-row-used ${secret.readable ? "" : "bad"}`}>{secret.readable ? used(secret) : t("secret.list.unreadable")}</span>
          <span className="secret-row-changed">{relTime(secret.updated_at)}</span>
        </button>
        {chat && (canAttach
          ? <IconButton className={`flat secret-row-attach ${isAttached ? "on" : ""}`} icon={isAttached ? "check" : "plus"} pressed={isAttached}
              label={t(isAttached ? "secret.detach.row" : "secret.attach.row", { name: secret.name })} onClick={() => toggle(secret)} />
          : <span className="secret-row-attach-none" aria-hidden="true" />)}
      </div>
    );
  };

  const total = all.length;
  const tabs = chat
    ? [
        { id: "chat" as const, label: <>{t("secret.tab.chat")}<span className="secrets-tab-n">{counts.chat}</span></> },
        ...(projectId ? [{ id: "project" as const, label: <>{t("secret.tab.project")}<span className="secrets-tab-n">{counts.project}</span></> }] : []),
        { id: "all" as const, label: <>{t("secret.tab.all")}<span className="secrets-tab-n">{counts.all}</span></> },
      ]
    : [];
  const pickTab = (next: SecretTab) => {
    setTab(next);
    if (layout !== "split") return;
    const first = sortSecrets(all.filter((s) => inTab(s, next, sessionId, projectId) && matchesSecret(s, query)), sort)[0];
    if (selected !== "new" && (!open || !inTab(open, next, sessionId, projectId))) void choose(first?.id ?? null);
  };

  const empty = list !== null && visible.length === 0 && (
    total === 0 ? (
      <div className="secrets-empty" data-empty="none">
        <Icon name="key" size={22} />
        <b>{t("secret.empty.title")}</b>
        <span>{t("secret.empty.body")}</span>
        <Button icon="plus" variant={selected === "new" ? "default" : "primary"} onClick={() => void choose("new")}>{t("secret.new")}</Button>
      </div>
    ) : query.trim() ? (
      <div className="secrets-empty" data-empty="search">
        <b>{t("secret.search.none", { q: query.trim() })}</b>
        <Button onClick={() => { setQuery(""); search.current?.focus(); }}>{t("secret.search.clear")}</Button>
      </div>
    ) : (
      <div className="secrets-empty" data-empty={shownTab}>
        <b>{t(shownTab === "project" ? "secret.empty.project" : "secret.empty.chat")}</b>
        <span>{t(shownTab === "project" ? "secret.empty.project.sub" : "secret.empty.chat.sub")}</span>
        <Button icon="plus" onClick={() => void choose("new")}>{t("secret.new")}</Button>
      </div>
    )
  );

  const listColumn = (
    <div className="secrets-listcol">
      <div className="secrets-toolbar">
        <label className="secrets-search">
          <Icon name="search" size={16} />
          <input ref={search} type="search" className="field" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("secret.search")} aria-label={t("secret.search")} {...NO_AUTOFILL}
            onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); listBox.current?.querySelector<HTMLButtonElement>(".secret-row-open")?.focus(); } }} />
          {!phone && !query && <Kbd>/</Kbd>}
        </label>
      </div>
      {chat && <Segmented value={tab} options={tabs} onChange={pickTab} label={t("secret.tabs")} className={`secrets-tabs ${phone ? "ph-seg" : ""}`} />}
      <div className="secrets-meta">
        <span className="secrets-count">{list === null ? "" : plural("secret.count", visible.length)}</span>
        <Select value={sort} label={t("secret.sort")} className="secrets-sort" onChange={setSort}
          options={[{ id: "used", label: t("secret.sort.used") }, { id: "name", label: t("secret.sort.name") }, { id: "changed", label: t("secret.sort.changed") }]} />
      </div>
      <div className="secrets-cols" aria-hidden="true">
        <span>{t("secret.name")}</span><span>{t("secret.col.where")}</span><span>{t("secret.col.used")}</span><span>{t("secret.col.changed")}</span>
      </div>
      <div ref={listBox} className="secrets-list" role="list" aria-label={t("secret.title")} onKeyDown={walk}>
        {list === null && <div className="secrets-empty" data-empty="loading">{t("common.loading")}</div>}
        {empty}
        {groups
          ? groups.map((g) => (
              <div key={g.id || "-"} className="secrets-group" role="group" aria-label={g.title || t("secret.group.nochats")} data-group={g.id || "none"}>
                <div className="secrets-group-head"><Icon name={g.id ? "folder" : "chat"} size={12} /><span>{g.title || t("secret.group.nochats")}</span><span className="n">{g.secrets.length}</span></div>
                {g.secrets.map(row)}
              </div>
            ))
          : visible.map(row)}
      </div>
    </div>
  );

  const pane = selected === "new" ? (
    <NewSecret key="new" sessionId={sessionId} phone={phone} project={list?.project_name ?? null} hasProject={!!projectId} projects={list?.projects ?? []}
      attaching={!!onAttach} dirty={dirty} onKept={(s, attach) => void kept(s, attach)} titled={layout !== "sheet"} scrolled={layout !== "split"}
      onCancel={total > 0 || !chat ? () => void choose(layout === "split" ? visible[0]?.id ?? null : null) : undefined} onBack={layout === "push" ? () => void choose(null) : undefined} />
  ) : open ? (
    <SecretPane key={open.id} secret={open} chat={chat} sessionId={sessionId} attachable={chat && attachable.has(open.id)} attached={attached.includes(open.name)}
      onToggle={() => toggle(open)} revealed={revealed?.id === open.id ? revealed.value : undefined} onReveal={() => void reveal(open)} onCopy={() => void copy(open)}
      copied={copied === open.id} dirty={dirty} onSaved={(next) => saved(next, open)} onRemove={() => void askRemove(open)} titled={layout !== "sheet"} scrolled={layout !== "split"}
      onBack={layout === "push" ? () => void choose(null) : undefined} chatTitle={chatTitle} leave={leave} />
  ) : null;

  const placeholder = list !== null && total > 0 && (
    <div className="secret-pane secret-pane-none"><div className="secrets-empty"><Icon name="key" size={22} /><span>{t("secret.pick")}</span></div></div>
  );

  const newButton = !chat || phone ? null : (
    <Button size="sm" icon="plus" kbd="N" className="secrets-add-open" onClick={() => void choose("new")}>{t("secret.new")}</Button>
  );

  return (
    <div ref={root} className={`secrets-manager ${chat ? "in-chat" : "in-settings"} layout-${layout} ${layout === "split" && !pane && !placeholder ? "no-pane" : ""}`} data-layout={layout}>
      {layout === "push" && pane ? pane : (
        <>
          {newButton && <div className="secrets-head-actions">{newButton}</div>}
          {listColumn}
          {layout === "push" && <div className="secrets-push-actions sheet-foot"><Button icon="plus" variant="primary" className="secrets-add-open" onClick={() => void choose("new")}>{t("secret.new")}</Button></div>}
          {layout === "split" && (pane ?? placeholder)}
        </>
      )}
      {layout === "sheet" && pane && (
        <Sheet title={selected === "new" ? t("secret.new.title") : open?.name} size={phone ? "full" : "lg"} className={`secret-pane-sheet ${phone ? "ph-sheet" : ""}`} onClose={() => void choose(null)}>
          {pane}
        </Sheet>
      )}
    </div>
  );
}

/** The width of the manager's own box, for Settings, where the column is 700 to 1000 px and decides
 *  whether the open secret sits beside the list. */
function useWidth(ref: React.RefObject<HTMLElement | null>, on: boolean): number {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const box = ref.current;
    if (!on || !box) return;
    setWidth(box.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(() => setWidth(box.clientWidth));
    watch.observe(box);
    return () => watch.disconnect();
  }, [ref, on]);
  return width;
}

/** Where the pane scrolls with a sheet rather than inside its own column, its foot is the sheet's
 *  foot: it stays at the bottom edge, and the sheet stops drawing its "more below" fade and arrow
 *  over the buttons. */
function footClass(scrolled: boolean): string {
  return scrolled ? "secret-pane-foot sheet-foot" : "secret-pane-foot";
}

/** The pane's head: a phone's way back to the list, and the name it is about. */
function PaneHead({ titled, title, mono, onBack, children }: { titled: boolean; title: string; mono?: boolean; onBack?: () => void; children?: ReactNode }) {
  if (!titled && !onBack) return null;
  return (
    <header className="secret-pane-head">
      {onBack && <IconButton className="secret-pane-back" icon="back" label={t("secret.back")} onClick={onBack} />}
      {titled && <h2 className={`secret-pane-title ${mono ? "mono" : ""}`}>{title}</h2>}
      {children}
    </header>
  );
}

/** One secret, open: its value behind the eye, its name and note to change, a new value in place of
 *  the old one, where it can be used and where it was. */
function SecretPane({ secret, chat, sessionId, attachable, attached, onToggle, revealed, onReveal, onCopy, copied, dirty, onSaved, onRemove, titled, scrolled, onBack, chatTitle, leave }: {
  secret: SecretView;
  chat: boolean;
  sessionId?: string;
  attachable: boolean;
  attached: boolean;
  onToggle: () => void;
  revealed: string | undefined;
  onReveal: () => void;
  onCopy: () => void;
  copied: boolean;
  dirty: MutableRefObject<boolean>;
  onSaved: (secret: SecretView) => void;
  onRemove: () => void;
  titled: boolean;
  scrolled: boolean;
  onBack?: () => void;
  chatTitle: (id: string) => string;
  leave: (href: string) => void;
}) {
  const [name, setName] = useState(secret.name);
  const [note, setNote] = useState(secret.note);
  const [rotating, setRotating] = useState(!secret.readable);
  const [filled, setFilled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [usage, setUsage] = useState<SecretUsage | null>(null);
  const value = useRef<HTMLTextAreaElement | null>(null);
  const base = `secret-edit-${secret.id}`;

  useEffect(() => {
    let live = true;
    api.get<SecretUsage>(`/api/secrets/${secret.id}/usage`).then((got) => { if (live) setUsage(got); }).catch(() => { if (live) setUsage({ conversations: 0, titles: [], audit: [] }); });
    return () => { live = false; };
  }, [secret.id, secret.updated_at]);

  const okName = validSecretName(name);
  const change: Record<string, string> = {};
  if (okName && secretName(name) !== secret.name) change.name = secretName(name);
  if (note.trim() !== secret.note) change.note = note.trim();
  const changing = Object.keys(change).length > 0 || filled;
  dirty.current = changing;
  const ready = okName && changing && !busy;

  async function save() {
    if (!ready) return;
    const body = { ...change, ...(filled && value.current ? { value: value.current.value } : {}) };
    setBusy(true);
    setProblem("");
    try {
      const next = await api.patch<SecretView>(`/api/secrets/${secret.id}`, body);
      if (value.current) value.current.value = "";
      setFilled(false);
      setRotating(false);
      onSaved(next);
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const last = secret.last_used_by ? usedBy(secret.last_used_by) : null;
  const chatLink = (id: string, title: string) => (
    <a className="secret-link" href={pathFor("agents", id)} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); leave(pathFor("agents", id)); }}>
      <Icon name="chat" size={12} /><span>{title}</span>
    </a>
  );
  const audit: SecretAudit[] = usage?.audit ?? [];

  return (
    <section className="secret-pane" data-secret-pane={secret.name} aria-label={secret.name}>
      <PaneHead titled={titled} title={secret.name} mono onBack={onBack}><Where secret={secret} /></PaneHead>
      <div className="secret-pane-body">
        <p className="secret-sees">{t("secret.name.as", { placeholder: secret.placeholder })} · <code>${secret.env}</code></p>

        <div className="secret-block">
          <div className="secret-block-label">{t("secret.value")}</div>
          <div className="secret-valuebox">
            {secret.readable
              ? revealed === undefined
                ? <code className="secret-dots" aria-label={t("secret.row.hidden")}>{MASK}</code>
                : <pre className="secret-shown" aria-label={t("secret.value")}>{revealed}</pre>
              : <span className="secret-dots bad">{t("secret.list.unreadable")}</span>}
            <span className="secret-valuebox-tools">
              <IconButton className="flat secret-reveal" icon={revealed !== undefined ? "eyeoff" : "eye"} label={t(revealed !== undefined ? "secret.value.hide" : "secret.value.show")} pressed={revealed !== undefined} onClick={onReveal} disabled={!secret.readable} />
              <IconButton className="flat secret-copy" icon={copied ? "check" : "copy"} label={t("secret.row.copy")} onClick={onCopy} disabled={!secret.readable} />
            </span>
          </div>
          {revealed !== undefined && <div className="secret-timer" aria-hidden="true" key={revealed} />}
          {!rotating && <div><Button size="sm" icon="reload" className="secret-rotate-open" onClick={() => setRotating(true)}>{t("secret.rotate")}</Button></div>}
          {rotating && (
            <ValueField id={`${base}-value`} label={t("secret.value.new")} field={value} onFilled={setFilled} placeholder={t("secret.value.paste")}
              hint={t(secret.readable ? "secret.rotate.hint" : "secret.rotate.unreadable")} />
          )}
        </div>

        <Field id={`${base}-name`} label={t("secret.name")} mono value={name} onChange={(e) => setName(e.target.value)} {...NO_AUTOFILL} maxLength={64}
          hint={okName && secretName(name) !== secret.name ? t("secret.rename.hint", { placeholder: placeholderOf(secretName(name)) }) : undefined}
          error={!okName ? t("secret.name.bad") : undefined} />
        <Field id={`${base}-note`} label={t("secret.note")} multiline rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("secret.note.placeholder")} maxLength={500} />

        <div className="secret-block">
          <div className="secret-block-label">{t("secret.usage")}</div>
          <div className="secret-facts">
            <div className="secret-fact"><span className="k">{t("secret.scope")}</span><span className="v">{whereLabel(secret)}</span></div>
            <div className="secret-fact"><span className="k">{t("secret.fact.used")}</span>
              <span className="v">{secret.last_used_at
                ? <>{relTimeLong(secret.last_used_at)} · {plural("secret.fact.times", secret.uses)}</>
                : t("secret.list.unused")}</span>
            </div>
            {last && (
              <div className="secret-fact"><span className="k">{t("secret.fact.by")}</span>
                <span className="v">{last.tool}{last.session && <> · {chatLink(last.session, chatTitle(last.session))}</>}{last.site && <> · <span className="secret-site">{last.site}</span></>}</span>
              </div>
            )}
            <div className="secret-fact"><span className="k">{t("secret.fact.changed")}</span><span className="v" title={absTime(secret.updated_at)}>{timeAgo(secret.updated_at)}</span></div>
            <div className="secret-fact"><span className="k">{t("secret.fact.created")}</span><span className="v" title={absTime(secret.created_at)}>{timeAgo(secret.created_at)}</span></div>
            {chat && (
              <div className="secret-fact secret-fact-draft"><span className="k">{t("secret.fact.draft")}</span>
                <span className="v">{attachable ? t(attached ? "secret.fact.draft.on" : "secret.fact.draft.off") : t("secret.fact.draft.no")}</span>
              </div>
            )}
          </div>
        </div>

        <div className="secret-block">
          <div className="secret-block-label">{t("secret.named")}{usage && usage.conversations > 0 && <span className="n">{usage.conversations}</span>}</div>
          {usage === null
            ? <span className="sub">{t("common.loading")}</span>
            : usage.titles.length === 0
              ? <span className="sub">{t("secret.named.none")}</span>
              : <div className="secret-named">{usage.titles.map((c) => <span key={c.id}>{chatLink(c.id, c.title || chatTitle(c.id))}</span>)}{usage.conversations > usage.titles.length && <span className="sub">{t("secret.named.more", { n: String(usage.conversations - usage.titles.length) })}</span>}</div>}
        </div>

        {audit.length > 0 && (
          <div className="secret-block">
            <div className="secret-block-label">{t("secret.trail")}</div>
            <div className="secret-trail">
              {audit.map((a, i) => (
                <div key={`${a.at}-${i}`} className="secret-trail-row"><span className="when" title={absTime(a.at)}>{timeAgo(a.at)}</span><span className="what">{auditAction(a.action)}</span><span className="via">{a.via}</span></div>
              ))}
            </div>
          </div>
        )}
        {problem && <div className="field-error" role="alert">{problem}</div>}
      </div>
      <footer className={footClass(scrolled)}>
        <Button variant="quiet" icon="trash" className="secret-remove" onClick={onRemove}>{t("common.remove")}</Button>
        <span className="grow" />
        {chat && sessionId && attachable && <Button className="secret-foot-attach" icon={attached ? "check" : "plus"} onClick={onToggle}>{t(attached ? "secret.detach.short" : "secret.attach.short")}</Button>}
        <Button variant="primary" className="secret-save" disabled={!ready} busy={busy} onClick={() => void save()}>{t("secret.save")}</Button>
      </footer>
    </section>
  );
}

/** A secret being handed over: its name, its value, a note, and where it can be used. Over a chat that
 *  is the chat or its project; in Settings, one of the projects (a chat's own secret is added from it). */
function NewSecret({ sessionId, phone, project, hasProject, projects, attaching, dirty, onKept, onCancel, titled, scrolled, onBack }: {
  sessionId?: string;
  phone: boolean;
  project: string | null;
  hasProject: boolean;
  projects: { id: string; name: string }[];
  attaching: boolean;
  dirty: MutableRefObject<boolean>;
  onKept: (secret: SecretView, attach: boolean) => void;
  onCancel?: () => void;
  titled: boolean;
  scrolled: boolean;
  onBack?: () => void;
}) {
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [filled, setFilled] = useState(false);
  const [scope, setScope] = useState<SecretScope>("session");
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const value = useRef<HTMLTextAreaElement | null>(null);
  const okName = validSecretName(name);
  const filable = sessionId ? true : !!projectId;
  const ready = okName && filled && filable && !busy;
  dirty.current = !!name.trim() || filled || !!note.trim();
  useEffect(() => {
    if (!projectId && projects[0]) setProjectId(projects[0].id);
  }, [projects, projectId]);

  async function keep(attach: boolean) {
    if (!ready || !value.current) return;
    setBusy(true);
    setProblem("");
    try {
      const body = sessionId
        ? { session_id: sessionId, scope, name: secretName(name), value: value.current.value, note: note.trim() }
        : { project_id: projectId, scope: "project", name: secretName(name), value: value.current.value, note: note.trim() };
      const kept = await api.post<SecretView>("/api/secrets", body);
      value.current.value = "";
      setFilled(false);
      setName("");
      setNote("");
      dirty.current = false;
      onKept(kept, attach);
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const scopes = [
    { id: "session" as const, label: t("secret.scope.session") },
    ...(hasProject ? [{ id: "project" as const, label: t("secret.scope.project") }] : []),
  ];
  return (
    <section className="secret-pane secret-add" data-secret-add="" aria-label={t("secret.new.title")}>
      <PaneHead titled={titled} title={t("secret.new.title")} onBack={onBack} />
      <div className="secret-pane-body">
        <p className="sub secret-why">{t("secret.why")}</p>
        <Field id="secret-name" label={t("secret.name")} mono value={name} onChange={(e) => setName(e.target.value)} placeholder="router_admin" {...NO_AUTOFILL} maxLength={64}
          hint={t("secret.name.as", { placeholder: placeholderOf(name.trim() && okName ? secretName(name) : "…") })}
          error={name.trim() && !okName ? t("secret.name.bad") : undefined} />
        <ValueField id="secret-value" label={t("secret.value")} field={value} onFilled={setFilled} placeholder={t("secret.value.paste")} />
        <Field id="secret-note" label={t("secret.note")} multiline rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("secret.note.placeholder")} maxLength={500} />
        {sessionId && (
          <div className="field-box">
            <div className="field-label">{t("secret.scope")}</div>
            {scopes.length > 1
              ? <Segmented value={scope} options={scopes} onChange={setScope} label={t("secret.scope")} className={phone ? "ph-seg" : ""} />
              : <div className="secret-where-fixed">{t("secret.scope.session")}</div>}
            <div className="sub field-hint">{scope === "project" ? t("secret.scope.project.hint", { project: project || "" }) : t(scopes.length > 1 ? "secret.scope.session.hint" : "secret.scope.session.only")}</div>
          </div>
        )}
        {!sessionId && (
          projects.length > 0
            ? (
              <div className="field-box">
                <label className="field-label" htmlFor="secret-project">{t("secret.scope.pick")}</label>
                <Select value={projectId} options={projects.map((p) => ({ id: p.id, label: p.name }))} onChange={setProjectId} label={t("secret.scope.pick")} id="secret-project" size="md" />
                <div className="sub field-hint">{t("secret.scope.settings.hint")}</div>
              </div>
            )
            : <div className="sub field-hint">{t("secret.scope.noproject")}</div>
        )}
        {problem && <div className="field-error" role="alert">{problem}</div>}
      </div>
      <footer className={footClass(scrolled)}>
        {onCancel && <Button variant="quiet" className="secret-cancel" onClick={onCancel}>{t("common.cancel")}</Button>}
        <span className="grow" />
        {attaching && <Button className="secret-save-only" disabled={!ready} onClick={() => void keep(false)}>{t("secret.save")}</Button>}
        <Button variant="primary" className="secret-save" disabled={!ready} busy={busy} onClick={() => void keep(attaching)}>{t(attaching ? "secret.attach" : "secret.save")}</Button>
      </footer>
    </section>
  );
}

/** The composer's "+" → Secret: the manager in a wide sheet, attaching as its first use. A press on a
 *  row's mark puts the secret on the message or takes it off and leaves the sheet open; "Save and
 *  attach" on a new one closes it, as handing a first secret over is one motion. */
export function SecretSheet({ sessionId, phone, attached, onClose, onAttach, onDetach, onChanged }: {
  sessionId: string;
  phone: boolean;
  attached: string[];
  onClose: () => void;
  onAttach: (secret: AttachedSecret) => void;
  onDetach?: (id: string) => void;
  onChanged?: (id: string, name: string | null) => void;
}) {
  const guard = useRef<(() => Promise<boolean>) | null>(null);
  const close = async () => {
    if (await (guard.current?.() ?? Promise.resolve(true))) onClose();
  };
  const body = (
    <SecretsManager sessionId={sessionId} phone={phone} attached={attached} onChanged={onChanged} onDetach={onDetach} guard={guard}
      onAttach={(secret, done) => { onAttach(secret); if (done) onClose(); }}
      onLeave={(href) => void (guard.current?.() ?? Promise.resolve(true)).then((ok) => { if (!ok) return; onClose(); navigate(href); })} />
  );
  if (phone) return <Sheet title={t("secret.title")} onClose={() => void close()} className="ph-sheet secret-sheet" size="full">{body}</Sheet>;
  return <Sheet title={t("secret.title")} onClose={() => void close()} size="xl" className="secret-sheet">{body}</Sheet>;
}

/** A secret on a draft or a sent message: the lock and the name, never the value. */
export function SecretChip({ name, scope, onRemove }: { name: string; scope: SecretScope | string; onRemove?: () => void }) {
  return (
    <span className="secret-chip" data-scope={scope} title={t(scope === "project" ? "secret.chip.project" : "secret.chip.session", { name })}>
      <Icon name="lock" size={12} /><span>{name}</span>
      {onRemove && (
        <button type="button" className="secret-chip-x" onClick={onRemove} aria-label={t("common.remove")} title={t("common.remove")}>
          <Icon name="close" size={10} />
        </button>
      )}
    </span>
  );
}

/** Settings → Security: the manager over every secret, and the sign-ins the browser saved. "Add secret"
 *  is the section's header button, which bumps `requestNew`. */
export function SecretsCard({ toast: say, phone, requestNew }: { toast: (text: string) => void; phone: boolean; requestNew?: number }) {
  const [logins, setLogins] = useState<SavedLoginView[]>([]);
  const fetched = useCallback((list: SecretsList) => setLogins(list.logins ?? []), []);
  async function forget(login: SavedLoginView) {
    if (!(await confirmDialog({ title: t("secret.logins.remove.title", { site: login.site }), body: t("secret.logins.remove.body"), action: t("common.remove"), danger: true }))) return;
    try {
      await api.delete(`/api/secrets/logins/${login.id}`);
      setLogins((list) => list.filter((l) => l.id !== login.id));
    } catch (e) {
      say(errorText(e));
    }
  }
  return (
    <>
      <section className="set-group secrets-card" data-setting="security.secrets" aria-label={t("secret.list.title")}>
        <div className="set-label"><span>{t("secret.list.title")}</span></div>
        <p className="set-help">{t("secret.list.sub")}</p>
        <SecretsManager phone={phone} onFetched={fetched} requestNew={requestNew} />
      </section>
      {logins.length > 0 && (
        <SetGroup title={t("secret.logins.title")} help={t("secret.logins.sub")}>
          {logins.map((l) => (
            <Row
              key={l.id}
              className="secret-login"
              data-login={l.site}
              title={l.name}
              desc={<>{l.last_used_at ? t("secret.logins.used", { t: timeAgo(l.last_used_at) }) : t("secret.list.unused")}{!l.readable && <> · {t("secret.list.unreadable")}</>}</>}
            >
              <Button size="sm" onClick={() => void forget(l)}>{t("common.remove")}</Button>
            </Row>
          ))}
        </SetGroup>
      )}
    </>
  );
}
