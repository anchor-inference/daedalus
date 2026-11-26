// The operator's secrets in the app: the manager the composer's "+" opens and Settings shows, and the chip
// a message carries. A list never holds a value: a value is typed here and sent on its own request, and the
// one way back is the reveal route, asked for one secret at a time when the operator presses the eye or
// Copy, and dropped from memory again when it hides.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { Icon } from "./icons";
import { errorText } from "./ui";
import { Sheet, toast } from "./ui/dialogs";
import { timeAgo, useCopied } from "./ui/components";
import { Button, Field, IconButton, Segmented, Select, confirmDialog } from "./ui/primitives";
import { Row, SetBlock, SetGroup } from "./settingsrow";

export type SecretScope = "session" | "project";

/** A secret as the host shows it: everything but the value. */
export type SecretView = {
  id: string;
  name: string;
  scope: SecretScope;
  scope_id: string;
  scope_title?: string;
  note: string;
  placeholder: string;
  env: string;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  last_used_by: string;
  uses: number;
  readable: boolean;
};

/** What a draft carries: the name it goes by, and whether this composer made it (removing the chip
 *  then takes it back, as nothing else asked for it). */
export type AttachedSecret = { id: string; name: string; scope: SecretScope; fresh: boolean };

/** A sign-in saved from the browser's private form: the site and when, never the username or password. */
export type SavedLoginView = { id: string; site: string; name: string; created_at: string; updated_at: string; last_used_at: string | null; uses: number; readable: boolean };

export type SecretsList = {
  secrets: SecretView[];
  /** With a chat: every secret it cannot attach (another chat's, another project's), to manage from here. */
  others?: SecretView[];
  logins?: SavedLoginView[];
  /** Without a chat: the projects a new secret can be filed under. */
  projects?: { id: string; name: string }[];
  project_id?: string | null;
  project_name?: string | null;
};

type Usage = { conversations: number; titles: { id: string; title: string }[] };

/** The name as the host will keep it, shown while typing so the placeholder is no surprise. */
export function secretName(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s.-]+/g, "_");
}

export function validSecretName(raw: string): boolean {
  return /^[a-z][a-z0-9_]{0,47}$/.test(secretName(raw));
}

export const placeholderOf = (name: string) => `«secret:${name}»`;

/** How long a revealed value stays on screen before it hides by itself. */
export const REVEAL_MS = 30_000;
/** How long a removed secret can be brought back before the request goes out. */
export const UNDO_MS = 6000;
/** The dots a hidden value is drawn as: always as many, so the row says nothing of its length. */
const MASK = "••••••••••••";

/** Whether a secret answers a search: its name, its note, or the chat or project it belongs to. */
export function matchesSecret(secret: SecretView, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [secret.name, secret.note, secret.scope_title ?? ""].some((part) => part.toLowerCase().includes(q));
}

/** The masked value field with its eye: a textarea, because a certificate or a key has several lines. */
function ValueField({ id, label, value, onChange, placeholder }: { id: string; label: string; value: string; onChange: (value: string) => void; placeholder?: string }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="field-box">
      <label className="field-label" htmlFor={id}>{label}</label>
      <div className="secret-value-row">
        <textarea id={id} className={`field secret-value ${shown ? "shown" : ""}`} value={value} onChange={(e) => onChange(e.target.value)} rows={2} placeholder={placeholder}
          autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} data-1p-ignore="" data-lpignore="true" />
        <button type="button" className="iconbtn flat secret-eye" onClick={() => setShown((s) => !s)} aria-pressed={shown}
          aria-label={t(shown ? "secret.value.hide" : "secret.value.show")} title={t(shown ? "secret.value.hide" : "secret.value.show")}>
          <Icon name={shown ? "eyeoff" : "eye"} size={16} />
        </button>
      </div>
    </div>
  );
}

/** The form that adds a secret: name, value, note, and where it belongs. In a chat that is the chat or its
 *  project; in Settings, one of the projects (a chat's own secret is added from the chat). */
function AddForm({ sessionId, phone, project, projects, onKept, onCancel, attaching }: {
  sessionId?: string;
  phone: boolean;
  project: string | null;
  projects: { id: string; name: string }[];
  onKept: (secret: SecretView) => void;
  onCancel?: () => void;
  attaching: boolean;
}) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [scope, setScope] = useState<SecretScope>("session");
  const [projectId, setProjectId] = useState(projects[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const okName = validSecretName(name);
  const filable = sessionId ? true : !!projectId;
  const ready = okName && value.trim().length > 0 && filable && !busy;

  async function keep() {
    if (!ready) return;
    setBusy(true);
    setProblem("");
    try {
      const body = sessionId
        ? { session_id: sessionId, scope, name: secretName(name), value, note: note.trim() }
        : { project_id: projectId, scope: "project", name: secretName(name), value, note: note.trim() };
      const kept = await api.post<SecretView>("/api/secrets", body);
      setValue("");
      setName("");
      setNote("");
      onKept(kept);
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const scopes = [
    { id: "session" as const, label: t("secret.scope.session") },
    ...(project !== null ? [{ id: "project" as const, label: t("secret.scope.project") }] : []),
  ];
  return (
    <div className="secret-form secret-add" data-secret-add="">
      <Field id="secret-name" label={t("secret.name")} value={name} onChange={(e) => setName(e.target.value)} placeholder="router_admin"
        autoComplete="off" autoCapitalize="none" spellCheck={false} data-1p-ignore="" data-lpignore="true" maxLength={64}
        hint={name.trim() && okName ? t("secret.name.as", { placeholder: placeholderOf(secretName(name)) }) : undefined}
        error={name.trim() && !okName ? t("secret.name.bad") : undefined} />
      <ValueField id="secret-value" label={t("secret.value")} value={value} onChange={setValue} />
      <Field id="secret-note" label={t("secret.note")} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("secret.note.placeholder")} maxLength={500} />
      {sessionId && scopes.length > 1 && (
        <div className="field-box">
          <div className="field-label">{t("secret.scope")}</div>
          {phone
            ? <Segmented value={scope} options={scopes} onChange={setScope} label={t("secret.scope")} className="ph-seg" />
            : <Segmented value={scope} options={scopes} onChange={setScope} label={t("secret.scope")} />}
          {scope === "project" && <div className="sub field-hint">{t("secret.scope.project.hint", { project: project || "" })}</div>}
        </div>
      )}
      {!sessionId && (
        projects.length > 0
          ? (
            <div className="field-box">
              <label className="field-label" htmlFor="secret-project">{t("secret.scope.pick")}</label>
              <Select value={projectId} options={projects.map((p) => ({ id: p.id, label: p.name }))} onChange={setProjectId} label={t("secret.scope.pick")} id="secret-project" />
              <div className="sub field-hint">{t("secret.scope.settings.hint")}</div>
            </div>
          )
          : <div className="sub field-hint">{t("secret.scope.noproject")}</div>
      )}
      {problem && <div className="field-error" role="alert">{problem}</div>}
      <div className="secret-form-actions">
        {onCancel && <Button variant="quiet" onClick={onCancel}>{t("common.cancel")}</Button>}
        <Button variant="primary" disabled={!ready} onClick={() => void keep()}>{t(attaching ? "secret.attach" : "secret.save")}</Button>
      </div>
    </div>
  );
}

/** A secret changed in place: the name, the note, and a new value when one is typed. The current value is
 *  never fetched for this: an empty field keeps it. */
function EditForm({ secret, onSaved, onCancel }: { secret: SecretView; onSaved: (secret: SecretView) => void; onCancel: () => void }) {
  const [name, setName] = useState(secret.name);
  const [value, setValue] = useState("");
  const [note, setNote] = useState(secret.note);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const okName = validSecretName(name);
  const change: Record<string, string> = {};
  if (okName && secretName(name) !== secret.name) change.name = secretName(name);
  if (note.trim() !== secret.note) change.note = note.trim();
  if (value.trim()) change.value = value;
  const ready = okName && Object.keys(change).length > 0 && !busy;

  async function save() {
    if (!ready) return;
    setBusy(true);
    setProblem("");
    try {
      const saved = await api.patch<SecretView>(`/api/secrets/${secret.id}`, change);
      setValue("");
      onSaved(saved);
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const base = `secret-edit-${secret.id}`;
  return (
    <div className="secret-form secret-edit" data-secret-edit={secret.name}>
      <Field id={`${base}-name`} label={t("secret.name")} value={name} onChange={(e) => setName(e.target.value)}
        autoComplete="off" autoCapitalize="none" spellCheck={false} data-1p-ignore="" data-lpignore="true" maxLength={64}
        hint={okName && secretName(name) !== secret.name ? t("secret.rename.hint", { placeholder: placeholderOf(secretName(name)) }) : undefined}
        error={!okName ? t("secret.name.bad") : undefined} />
      <ValueField id={`${base}-value`} label={t("secret.value.new")} value={value} onChange={setValue} placeholder={t("secret.value.keep")} />
      <Field id={`${base}-note`} label={t("secret.note")} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("secret.note.placeholder")} maxLength={500} />
      {problem && <div className="field-error" role="alert">{problem}</div>}
      <div className="secret-form-actions">
        <Button variant="quiet" onClick={onCancel}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={!ready} onClick={() => void save()}>{t("secret.save")}</Button>
      </div>
    </div>
  );
}

function scopeLabel(secret: SecretView): string {
  return t(secret.scope === "project" ? "secret.list.project" : "secret.list.session", { title: secret.scope_title || secret.scope_id });
}

/** One secret as a row of the card it is listed in: its name, where it belongs and what it says, when it
 *  changed and was used; at the right the value as dots and what can be done with it. A revealed value
 *  takes the line under the row, for half a minute: a certificate has several lines and would not fit
 *  the lane. */
function SecretRow({ secret, attach, attached, revealed, onReveal, onCopy, copied, onEdit, onRemove, editing, onSaved, onCancelEdit }: {
  secret: SecretView;
  attach?: () => void;
  attached: boolean;
  revealed: string | undefined;
  onReveal: () => void;
  onCopy: () => void;
  copied: boolean;
  onEdit: () => void;
  onRemove: () => void;
  editing: boolean;
  onSaved: (secret: SecretView) => void;
  onCancelEdit: () => void;
}) {
  if (editing) return <SetBlock className="secret-row editing" data-secret={secret.name}><EditForm secret={secret} onSaved={onSaved} onCancel={onCancelEdit} /></SetBlock>;
  const shown = revealed !== undefined;
  const used = secret.last_used_at
    ? t("secret.row.used", { t: timeAgo(secret.last_used_at), n: String(secret.uses), by: secret.last_used_by })
    : t("secret.list.unused");
  return (
    <div className="secret-row" data-secret={secret.name} data-scope={secret.scope}>
      <Row
        title={<span className="secret-row-name">{secret.name}</span>}
        desc={
          <>
            <span className="secret-row-scope" title={scopeLabel(secret)}>{scopeLabel(secret)}</span>
            {secret.note && <> · <span className="secret-row-note">{secret.note}</span></>}
            <span className="secret-row-meta">{t("secret.row.updated", { t: timeAgo(secret.updated_at) })} · {used}</span>
          </>
        }
      >
        {attach && (
          attached
            ? <span className="secret-row-attached"><Icon name="check" size={12} />{t("secret.row.attached")}</span>
            : <Button size="sm" variant="primary" className="secret-row-attach" onClick={attach}>{t("secret.row.attach")}</Button>
        )}
        {!shown && (secret.readable
          ? <code className="secret-row-dots" aria-label={t("secret.row.hidden")}>{MASK}</code>
          : <span className="secret-row-dots bad">{t("secret.list.unreadable")}</span>)}
        <span className="secret-row-tools">
          <IconButton className="flat secret-reveal" icon={shown ? "eyeoff" : "eye"} label={t(shown ? "secret.value.hide" : "secret.value.show")} pressed={shown} onClick={onReveal} disabled={!secret.readable} />
          <IconButton className="flat secret-copy" icon={copied ? "check" : "copy"} label={t("secret.row.copy")} onClick={onCopy} disabled={!secret.readable} />
          <IconButton className="flat secret-edit-open" icon="pen" label={t("secret.row.edit")} onClick={onEdit} />
          <IconButton className="flat danger secret-remove" icon="trash" label={t("common.remove")} onClick={onRemove} />
        </span>
      </Row>
      {shown && (
        <div className="secret-revealed">
          <code className="secret-row-dots shown" aria-label={t("secret.value")}>{revealed}</code>
          <div className="secret-timer" aria-hidden="true" key={revealed} />
        </div>
      )}
    </div>
  );
}

/** The secrets manager: a search, "Add secret", and the list. With a chat (`sessionId`) the chat's own and
 *  its project's secrets come first and each can be attached, which is the point of opening it from the
 *  composer; the others follow, folded, to change or remove. Without one (Settings, drawn as `settings`:
 *  a group of the page rather than the body of a sheet) it is every secret. */
export function SecretsManager({ sessionId, phone, settings, attached = [], onAttach, onChanged, onFetched }: {
  sessionId?: string;
  phone: boolean;
  settings?: boolean;
  attached?: string[];
  onAttach?: (secret: AttachedSecret) => void;
  /** A secret was renamed (its new name) or removed (null): a draft holding it follows. */
  onChanged?: (id: string, name: string | null) => void;
  onFetched?: (list: SecretsList) => void;
}) {
  const [list, setList] = useState<SecretsList | null>(null);
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState<boolean | null>(null);
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const [othersOpen, setOthersOpen] = useState(!sessionId);
  const hideTimers = useRef(new Map<string, number>());
  const removals = useRef(new Map<string, number>());

  const load = useCallback(() => {
    const path = sessionId ? `/api/secrets?session_id=${encodeURIComponent(sessionId)}` : "/api/secrets";
    return api.get<SecretsList>(path)
      .then((got) => { setList(got); onFetched?.(got); return got; })
      .catch((e) => { setList({ secrets: [] }); if (!sessionId) toast(errorText(e)); return null; });
  }, [sessionId, onFetched]);

  useEffect(() => {
    let live = true;
    void load().then((got) => {
      // An empty chat opens on the form: the quickest way to hand a first secret over is still open, type, attach.
      if (live && got && sessionId && got.secrets.length === 0) setAdding((open) => open ?? true);
    });
    return () => { live = false; };
  }, [load, sessionId]);

  // Leaving takes every revealed value out of memory with it, and sends the removals still waiting for
  // their undo: closing the sheet is not taking the removal back.
  useEffect(() => {
    const timers = hideTimers.current;
    const pending = removals.current;
    return () => {
      timers.forEach((timer) => window.clearTimeout(timer));
      timers.clear();
      pending.forEach((timer, id) => {
        window.clearTimeout(timer);
        void api.delete(`/api/secrets/${id}`).catch(() => {});
      });
      pending.clear();
    };
  }, []);

  const hide = useCallback((id: string) => {
    const timer = hideTimers.current.get(id);
    if (timer) window.clearTimeout(timer);
    hideTimers.current.delete(id);
    setRevealed((all) => {
      if (!(id in all)) return all;
      const rest = { ...all };
      delete rest[id];
      return rest;
    });
  }, []);

  async function reveal(secret: SecretView) {
    if (secret.id in revealed) return hide(secret.id);
    try {
      const { value } = await api.post<{ value: string }>(`/api/secrets/${secret.id}/reveal`, { purpose: "reveal" });
      setRevealed((all) => ({ ...all, [secret.id]: value }));
      hideTimers.current.set(secret.id, window.setTimeout(() => hide(secret.id), REVEAL_MS));
    } catch (e) {
      toast(errorText(e));
    }
  }

  const [copied, copyValue] = useCopied();
  async function copy(secret: SecretView) {
    try {
      const { value } = await api.post<{ value: string }>(`/api/secrets/${secret.id}/reveal`, { purpose: "copy" });
      if (await copyValue(value, secret.id)) return;
      toast(t("secret.row.copy.failed"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function askRemove(secret: SecretView) {
    // Not knowing where it is named is no reason to refuse removing it; the warning is a courtesy.
    const usage = await api.get<Usage>(`/api/secrets/${secret.id}/usage`).catch((): Usage => ({ conversations: 0, titles: [] }));
    const sure = await confirmDialog({
      title: t("secret.remove.title", { name: secret.name }),
      body: (
        <>
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
    hide(secret.id);
    setGone((all) => new Set(all).add(secret.id));
    onChanged?.(secret.id, null);
    const timer = window.setTimeout(() => {
      removals.current.delete(secret.id);
      api.delete(`/api/secrets/${secret.id}`)
        .then(() => setList((cur) => cur && { ...cur, secrets: cur.secrets.filter((s) => s.id !== secret.id), others: cur.others?.filter((s) => s.id !== secret.id) }))
        .catch((e) => {
          setGone((all) => { const next = new Set(all); next.delete(secret.id); return next; });
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
        setGone((all) => { const next = new Set(all); next.delete(secret.id); return next; });
      },
    });
  }

  function saved(secret: SecretView, previous: SecretView) {
    setEditing(null);
    hide(secret.id);
    const merge = (items?: SecretView[]) => items?.map((s) => (s.id === secret.id ? { ...s, ...secret, scope_title: s.scope_title } : s));
    setList((cur) => cur && { ...cur, secrets: merge(cur.secrets) ?? [], others: merge(cur.others) });
    if (secret.name !== previous.name) onChanged?.(secret.id, secret.name);
  }

  function kept(secret: SecretView) {
    if (onAttach) {
      onAttach({ id: secret.id, name: secret.name, scope: secret.scope, fresh: true });
      return;
    }
    setAdding(false);
    void load();
  }

  const own = useMemo(() => (list?.secrets ?? []).filter((s) => !gone.has(s.id)), [list, gone]);
  const others = useMemo(() => (list?.others ?? []).filter((s) => !gone.has(s.id)), [list, gone]);
  const total = own.length + others.length;
  const ownShown = own.filter((s) => matchesSecret(s, query));
  const othersShown = others.filter((s) => matchesSecret(s, query));
  const project = list?.project_id ? list.project_name || "" : null;

  const row = (secret: SecretView, attachable: boolean) => (
    <SecretRow key={secret.id} secret={secret}
      attach={attachable && onAttach ? () => onAttach({ id: secret.id, name: secret.name, scope: secret.scope, fresh: false }) : undefined}
      attached={attached.includes(secret.name)}
      revealed={revealed[secret.id]}
      onReveal={() => void reveal(secret)}
      onCopy={() => void copy(secret)}
      copied={copied === secret.id}
      onEdit={() => setEditing(secret.id)}
      onRemove={() => void askRemove(secret)}
      editing={editing === secret.id}
      onSaved={(next) => saved(next, secret)}
      onCancelEdit={() => setEditing(null)}
    />
  );

  const adder = !adding && (
    <Button size={settings ? "sm" : "md"} icon="plus" className="secrets-add-open" onClick={() => { setEditing(null); setAdding(true); }}>{t("secret.add")}</Button>
  );
  const search = total > 0 && (
    <label className="secrets-search">
      <Icon name="search" size={14} />
      <input type="search" className="field" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("secret.search")} aria-label={t("secret.search")} />
    </label>
  );
  const empty = list !== null && total === 0 && !adding && t(sessionId ? "secret.list.none.chat" : "secret.list.none");
  const noMatch = list !== null && total > 0 && ownShown.length + othersShown.length === 0 && t("secret.search.none");

  if (settings) {
    return (
      <SetGroup title={t("secret.list.title")} help={t("secret.list.sub")} action={adder} className="secrets-card" data-setting="security.secrets">
        {adding && <SetBlock><AddForm sessionId={sessionId} phone={phone} project={project} projects={list?.projects ?? []} attaching={false} onKept={kept} onCancel={() => setAdding(false)} /></SetBlock>}
        {search && <SetBlock className="secrets-toolbar">{search}</SetBlock>}
        {list === null && <SetBlock><span className="set-hint">{t("common.loading")}</span></SetBlock>}
        {empty && <SetBlock><span className="set-hint secrets-none">{empty}</span></SetBlock>}
        {noMatch && <SetBlock><span className="set-hint secrets-none">{noMatch}</span></SetBlock>}
        {ownShown.map((s) => row(s, false))}
      </SetGroup>
    );
  }

  return (
    <div className="secrets-manager">
      {sessionId && <p className="sub secret-why">{t("secret.why")}</p>}
      <div className="secrets-toolbar">
        {search}
        {adder}
      </div>
      {adding && (
        <AddForm sessionId={sessionId} phone={phone} project={project} projects={list?.projects ?? []} attaching={!!onAttach} onKept={kept}
          onCancel={total > 0 || !sessionId ? () => setAdding(false) : undefined} />
      )}
      {list === null && <div className="empty">{t("common.loading")}</div>}
      {empty && <div className="sub secrets-none">{empty}</div>}
      {noMatch && <div className="sub secrets-none">{noMatch}</div>}
      {ownShown.length > 0 && (
        <div className="secrets-group">
          {sessionId && <div className="secret-heading">{t("secret.group.here")}</div>}
          <div className="secrets-list set-card">{ownShown.map((s) => row(s, !!sessionId))}</div>
        </div>
      )}
      {othersShown.length > 0 && (
        <div className="secrets-group secrets-others">
          <button type="button" className="secret-heading secrets-others-toggle" aria-expanded={othersOpen || !!query.trim()} onClick={() => setOthersOpen((open) => !open)}>
            <Icon name="chevron" size={12} />{t("secret.group.others", { n: String(othersShown.length) })}
          </button>
          {(othersOpen || !!query.trim()) && <div className="secrets-list set-card">{othersShown.map((s) => row(s, false))}</div>}
        </div>
      )}
    </div>
  );
}

/** The composer's "+" → Secret: the manager, with attaching as its first action. A sheet on a desktop and
 *  a bottom sheet on a phone. */
export function SecretSheet({ sessionId, phone, attached, onClose, onAttach, onChanged }: {
  sessionId: string;
  phone: boolean;
  attached: string[];
  onClose: () => void;
  onAttach: (secret: AttachedSecret) => void;
  onChanged?: (id: string, name: string | null) => void;
}) {
  const body = (
    <SecretsManager sessionId={sessionId} phone={phone} attached={attached} onChanged={onChanged}
      onAttach={(secret) => { onAttach(secret); onClose(); }} />
  );
  if (phone) return <Sheet title={t("secret.title")} onClose={onClose} className="ph-sheet secret-sheet" size="full">{body}</Sheet>;
  return <Sheet title={t("secret.title")} onClose={onClose} size="sm" className="secret-sheet">{body}</Sheet>;
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

/** Settings → Security: the same manager over every secret, and the sign-ins the browser saved. */
export function SecretsCard({ toast: say }: { toast: (text: string) => void }) {
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
      <SecretsManager phone={false} settings onFetched={fetched} />
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
