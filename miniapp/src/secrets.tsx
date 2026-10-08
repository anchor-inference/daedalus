// The operator's secrets in the app: the form the composer's "+" opens, the chip a message carries, and
// the list Settings keeps. A value is typed here once, sent on its own request, and never comes back: the
// app only ever holds a secret's name, note and scope.
import { useEffect, useState } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { Icon } from "./icons";
import { errorText } from "./ui";
import { Sheet } from "./ui/dialogs";
import { Segmented } from "./ui/components";
import { BottomSheet, SegmentedControl } from "./ui/phone";

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

export type SecretsList = { secrets: SecretView[]; project_id?: string | null; project_name?: string | null };

/** The name as the host will keep it, shown while typing so the placeholder is no surprise. */
export function secretName(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s.-]+/g, "_");
}

export function validSecretName(raw: string): boolean {
  return /^[a-z][a-z0-9_]{0,47}$/.test(secretName(raw));
}

export const placeholderOf = (name: string) => `«secret:${name}»`;

/** The composer's "+" → Secret: name, a masked value (several lines are fine), a note for the agent, and
 *  whether it is for this chat or the whole project. Below, the secrets the chat already has, to attach
 *  one again without typing it. */
export function SecretSheet({ sessionId, phone, attached, onClose, onAttach }: {
  sessionId: string;
  phone: boolean;
  attached: string[];
  onClose: () => void;
  onAttach: (secret: AttachedSecret) => void;
}) {
  const [known, setKnown] = useState<SecretsList | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [scope, setScope] = useState<SecretScope>("session");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    let live = true;
    api.get<SecretsList>(`/api/secrets?session_id=${encodeURIComponent(sessionId)}`)
      .then((list) => { if (live) setKnown(list); })
      .catch(() => { if (live) setKnown({ secrets: [] }); });
    return () => { live = false; };
  }, [sessionId]);
  const project = known?.project_id ? known.project_name || "" : null;
  const okName = validSecretName(name);
  const ready = okName && value.trim().length > 0 && !busy;

  async function keep() {
    if (!ready) return;
    setBusy(true);
    setProblem("");
    try {
      const kept = await api.post<SecretView>("/api/secrets", { session_id: sessionId, scope, name: secretName(name), value, note: note.trim() });
      setValue("");
      onAttach({ id: kept.id, name: kept.name, scope: kept.scope, fresh: true });
      onClose();
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
  const reuse = (known?.secrets ?? []).filter((s) => !attached.includes(s.name));
  const body = (
    <div className="secret-form">
      <p className="sub secret-why">{t("secret.why")}</p>
      <label className="field" htmlFor="secret-name">{t("secret.name")}</label>
      <input id="secret-name" className="field" value={name} onChange={(e) => setName(e.target.value)} placeholder="router_admin"
        autoComplete="off" autoCapitalize="none" spellCheck={false} data-1p-ignore="" data-lpignore="true" maxLength={64} />
      {name.trim() && <div className={`sub secret-hint ${okName ? "" : "bad"}`}>{okName ? t("secret.name.as", { placeholder: placeholderOf(secretName(name)) }) : t("secret.name.bad")}</div>}
      <label className="field" htmlFor="secret-value">{t("secret.value")}</label>
      <div className="secret-value-row">
        <textarea id="secret-value" className={`field secret-value ${shown ? "shown" : ""}`} value={value} onChange={(e) => setValue(e.target.value)} rows={2}
          autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} data-1p-ignore="" data-lpignore="true" />
        <button type="button" className="iconbtn flat secret-eye" onClick={() => setShown((s) => !s)} aria-pressed={shown}
          aria-label={t(shown ? "secret.value.hide" : "secret.value.show")} title={t(shown ? "secret.value.hide" : "secret.value.show")}>
          <Icon name="eye" size={16} />
        </button>
      </div>
      <label className="field" htmlFor="secret-note">{t("secret.note")}</label>
      <input id="secret-note" className="field" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("secret.note.placeholder")} maxLength={500} />
      {scopes.length > 1 && (
        <>
          <div className="secret-heading">{t("secret.scope")}</div>
          {phone
            ? <SegmentedControl value={scope} options={scopes} onChange={setScope} label={t("secret.scope")} />
            : <Segmented value={scope} options={scopes} onChange={setScope} label={t("secret.scope")} />}
          {scope === "project" && <div className="sub secret-hint">{t("secret.scope.project.hint", { project: project || "" })}</div>}
        </>
      )}
      {problem && <div className="sub secret-hint bad" role="alert">{problem}</div>}
      {reuse.length > 0 && (
        <div className="secret-reuse">
          <div className="secret-heading">{t("secret.reuse")}</div>
          <div className="secret-chips">
            {reuse.map((s) => (
              <button key={s.id} type="button" className="secret-chip" onClick={() => { onAttach({ id: s.id, name: s.name, scope: s.scope, fresh: false }); onClose(); }}
                title={s.note || s.placeholder}>
                <Icon name="lock" size={12} /><span>{s.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
  const foot = (
    <>
      <button type="button" className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
      <button type="button" className="btn primary" disabled={!ready} onClick={() => void keep()}>{t("secret.attach")}</button>
    </>
  );
  if (phone) return <BottomSheet title={t("secret.title")} onClose={onClose} className="secret-sheet" footer={foot}>{body}</BottomSheet>;
  return (
    <Sheet title={t("secret.title")} onClose={onClose} size="narrow" className="secret-sheet">
      {body}
      <div className="sheet-foot">{foot}</div>
    </Sheet>
  );
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

/** Settings → Security: every secret handed over, where it belongs, who used it last; each can be taken back. */
export function SecretsCard({ toast, confirm, ago }: { toast: (text: string) => void; confirm: (title: string, body: string) => Promise<boolean>; ago: (iso: string) => string }) {
  const [items, setItems] = useState<SecretView[] | null>(null);
  useEffect(() => {
    api.get<SecretsList>("/api/secrets").then((list) => setItems(list.secrets)).catch((e) => { setItems([]); toast(errorText(e)); });
  }, [toast]);
  async function remove(secret: SecretView) {
    if (!(await confirm(t("secret.remove.title", { name: secret.name }), t("secret.remove.body")))) return;
    try {
      await api.delete(`/api/secrets/${secret.id}`);
      setItems((list) => (list ?? []).filter((s) => s.id !== secret.id));
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card secrets-card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("secret.list.title")}</div>
      <div className="sub">{t("secret.list.sub")}</div>
      {items === null && <div className="empty">{t("common.loading")}</div>}
      {items !== null && items.length === 0 && <div className="sub" style={{ marginTop: 8 }}>{t("secret.list.none")}</div>}
      {items !== null && items.length > 0 && (
        <div className="mlist">
          {items.map((s) => (
            <div key={s.id} className="mrow" data-secret={s.name}>
              <div className="mline noradio">
                <div className="mmain">
                  <span className="mtitle"><Icon name="lock" size={12} /> {s.name}</span>
                  <span className="mmeta">
                    {t(s.scope === "project" ? "secret.list.project" : "secret.list.session", { title: s.scope_title || s.scope_id })}
                    {" · "}
                    {s.last_used_at ? t("secret.list.used", { t: ago(s.last_used_at), by: s.last_used_by }) : t("secret.list.unused")}
                    {!s.readable && <> · {t("secret.list.unreadable")}</>}
                  </span>
                  {s.note && <span className="mmeta">{s.note}</span>}
                </div>
                <div className="mactions">
                  <button className="btn small" onClick={() => void remove(s)}>{t("common.remove")}</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
