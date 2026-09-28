import { useEffect, useState } from "react";
import { int, relTime, usd } from "./format";
import { api, LoopView, ServiceView, ShareMode, ToolInfo } from "./api";
import { Icon } from "./icons";
import { OverflowMenu, Sheet } from "./dialogs";
import { confirmAsync, errorText } from "./ui";
import { LANGS, t, useLang } from "./i18n";

export type Status = "idle" | "running" | "waiting" | "failed" | "done" | "compacting";

/** Two words, one chosen: the language every screen of the app is written in. */
export function LangPicker() {
  const [lang, pick] = useLang();
  return (
    <div className="segmented inline lang" role="group" aria-label={t("lang.pick")}>
      {LANGS.map((l) => (
        <button key={l} className={lang === l ? "on" : ""} aria-pressed={lang === l} onClick={() => pick(l)} title={t(`lang.name.${l}`)} aria-label={t(`lang.name.${l}`)}>
          {l.toUpperCase()}
        </button>
      ))}
    </div>
  );
}

/** One of a small fixed set, picked in place: the one choice control Settings uses, so a mode, an
 *  approval rule and an on/off setting all look and answer the same way. */
export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { id: T; label: string }[]; onChange: (id: T) => void; label?: string }) {
  return (
    <div className="segmented inline" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button key={option.id} type="button" role="radio" aria-checked={value === option.id} className={value === option.id ? "on" : ""} onClick={() => value !== option.id && onChange(option.id)}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

/** An on/off setting that takes effect at once. It is a switch and not a button labelled "on" or "off",
 *  because such a button beside an action such as "Run now" read as a second action, and nobody
 *  could tell whether its word was the state or what pressing it would do. */
export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} className={`switch ${checked ? "on" : ""}`} disabled={disabled} onClick={() => onChange(!checked)}>
      <span className="switch-track" aria-hidden><span className="switch-knob" /></span>
    </button>
  );
}

export function Avatar({ status, seed }: { status: Status; seed: string }) {
  // Deterministic accessory per session so each "bot" stays recognisable.
  const hue = [...seed].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const accessory = hue % 3;
  return (
    <svg className={`avatar ${status}`} viewBox="0 0 44 44" aria-label={status}>
      <rect className="body" x="4" y="8" width="36" height="30" rx="10" strokeWidth="1.5" />
      {accessory === 0 && <circle cx="22" cy="6" r="2.5" fill={`hsl(${hue} 70% 60%)`} />}
      {accessory === 1 && <rect x="12" y="3" width="20" height="4" rx="2" fill={`hsl(${hue} 70% 60%)`} />}
      {accessory === 2 && <path d="M8 10 L14 3 L20 10" fill="none" stroke={`hsl(${hue} 70% 60%)`} strokeWidth="2" />}
      <circle className="eye" cx="16" cy="22" r="3.2" />
      <circle className="eye" cx="28" cy="22" r="3.2" />
    </svg>
  );
}

/** The states the app has a word for; anything else the server invents is shown as it came. */
const STATUS_WORDS = ["idle", "running", "waiting", "compacting", "failed", "done", "paused", "stopped", "pending", "merged", "approved", "rejected", "closed", "dead"];

/** One word for a state, in the one colour that state has everywhere: idle stays grey. */
export function statusWord(status: string): string {
  return STATUS_WORDS.includes(status) ? t(`status.${status}`) : status;
}

/** The one disclosure glyph: a chevron that turns to point down when what it guards is open. */
export function Chevron({ open, size = 14 }: { open: boolean; size?: number }) {
  return (
    <span className={`chev ${open ? "down" : ""}`} aria-hidden>
      <Icon name="forward" size={size} />
    </span>
  );
}

export function Dot({ status, className }: { status: string; className?: string }) {
  return <span className={`dot ${status} ${className ?? ""}`} aria-hidden />;
}

/** A dot and the word: "● Working", "● Needs you". */
export function StatusLabel({ status, word }: { status: string; word?: string }) {
  return (
    <span className={`status ${status}`}>
      <span className="dot" aria-hidden />
      {word ?? statusWord(status)}
    </span>
  );
}

export function Pill({ status, children }: { status: string; children?: React.ReactNode }) {
  return <span className={`pill ${status}`}>{children ?? statusWord(status)}</span>;
}

/** A row of grey lines while the first load is on its way. */
export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="sk-row">
          <div className="skeleton avatar" />
          <div>
            <div className="skeleton line" style={{ width: `${55 + ((i * 17) % 30)}%` }} />
            <div className="skeleton line" style={{ width: `${30 + ((i * 11) % 25)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Checkboxes for the host tools, grouped, all on by default; collapsed until the operator opens it. */
export function ToolPicker({ off, onChange, note }: { off: string[]; onChange: (off: string[]) => void; note?: string }) {
  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open || tools !== null) return;
    api.get<ToolInfo[]>("/api/tools").then(setTools).catch(() => setTools([]));
  }, [open, tools]);
  const offSet = new Set(off);
  const groups = new Map<string, ToolInfo[]>();
  for (const t of tools ?? []) groups.set(t.group, [...(groups.get(t.group) ?? []), t]);
  const toggle = (name: string) => onChange(offSet.has(name) ? off.filter((n) => n !== name) : [...off, name]);
  const toggleGroup = (items: ToolInfo[]) => {
    const allOn = items.every((t) => !offSet.has(t.name));
    const names = items.map((t) => t.name);
    onChange(allOn ? [...off, ...names.filter((n) => !offSet.has(n))] : off.filter((n) => !names.includes(n)));
  };
  return (
    <div className="toolpicker">
      <button type="button" className="toolpicker-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`chev ${open ? "down" : ""}`}>›</span> {t("tools.title")}{off.length ? t("tools.off", { n: off.length }) : t("tools.allon")}
      </button>
      {open && tools === null && <div className="sub">{t("common.loading")}</div>}
      {open && tools !== null && (
        <div className="toolpicker-body">
          {note && <div className="sub" style={{ marginBottom: 6 }}>{note}</div>}
          {[...groups.entries()].map(([group, items]) => (
            <div key={group} className="toolgroup">
              <label className="toolrow head">
                <input type="checkbox" checked={items.every((t) => !offSet.has(t.name))} ref={(el) => { if (el) el.indeterminate = items.some((t) => offSet.has(t.name)) && !items.every((t) => offSet.has(t.name)); }} onChange={() => toggleGroup(items)} />
                <span>{group}</span>
              </label>
              {items.map((t) => (
                <label key={t.name} className="toolrow" title={t.description}>
                  <input type="checkbox" checked={!offSet.has(t.name)} onChange={() => toggle(t.name)} />
                  <span className="mono">{t.name}</span>
                  <span className="sub">{t.description}</span>
                </label>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function fmtInterval(seconds: number | null | undefined): string {
  if (!seconds) return t("tools.interval.dynamic");
  for (const [size, key] of [[86400, "fmt.dur.d"], [3600, "fmt.dur.h"], [60, "fmt.dur.m"]] as [number, string][]) if (seconds >= size && seconds % size === 0) return t(key, { n: seconds / size });
  return t("fmt.dur.s", { n: seconds });
}

/** "loop · 10m · #12" / "loop · paused" — the short form of a session's loop. */
export function loopLabel(loop: LoopView | null | undefined): string {
  if (!loop) return "";
  const cadence = loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced");
  const status = loop.status === "active" ? "" : ` · ${statusWord(loop.status).toLowerCase()}`;
  return `${t("loop.word")} · ${cadence} · #${loop.run_count}${loop.max_runs ? "/" + loop.max_runs : ""}${status}`;
}

export function timeAgo(iso: string | null | undefined): string {
  return relTime(iso);
}

export const fmtUsd = usd;
export const fmtInt = int;

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // No clipboard API (http, Telegram's webview): a hidden field and the old copy command still
    // work there. Where even that is refused the caller says so, and the text stays selectable.
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    field.remove();
    return ok;
  }
}

const accessWord = (mode: ShareMode) => t(`svc.access.${mode}`);

/** One hosted service: what it is, whether it runs, where to open it, and the rest behind a menu. */
export function ServiceRow({ s, sessionId, onChange, toast, onLogs, card }: { s: ServiceView; sessionId: string; onChange: () => void; toast: (t: string) => void; onLogs: (text: string) => void; card?: boolean }) {
  const [share, setShare] = useState(false);
  const [busy, setBusy] = useState(false);
  async function stop() {
    if (!(await confirmAsync(t("svc.stop.title", { name: s.name }), { body: t("svc.stop.body"), action: t("common.stop") }))) return;
    try {
      await api.post(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/stop`);
      toast(t("svc.stopped", { name: s.name }));
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove() {
    if (!(await confirmAsync(t(s.status === "running" ? "svc.remove.title.running" : "svc.remove.title", { name: s.name }), { body: t("svc.remove.body"), action: t("common.remove") }))) return;
    try {
      await api.delete(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}`);
      toast(t("svc.removed", { name: s.name }));
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function logs() {
    try {
      const r = await api.get<{ text: string }>(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/logs?lines=200`);
      onLogs(r.text);
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function setMode(mode: ShareMode, rotate = false) {
    if (mode === "public" && s.share?.mode !== "public" && !(await confirmAsync(t("svc.public.title", { name: s.name }), { body: t("svc.public.body"), action: t("svc.public.action") }))) return;
    setBusy(true);
    try {
      await api.post(`/api/sessions/${sessionId}/services/${encodeURIComponent(s.name)}/share`, { mode, rotate_key: rotate });
      toast(t(mode === "local" ? "svc.mode.local" : mode === "public" ? "svc.mode.public" : rotate ? "svc.mode.newkey" : "svc.mode.key", { name: s.name }));
      onChange();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function copy(text: string, what: "address" | "link" | "key") {
    toast((await copyText(text)) ? t(`svc.copied.${what}`) : t("svc.copyfail"));
  }
  const mode = s.share?.mode ?? "local";
  const shared = mode !== "local" && s.status === "running";
  const openUrl = shared && s.share?.url ? s.share.url : s.url;
  const lan = s.url?.replace(/^https?:\/\//, "");
  const menu = (
    <OverflowMenu
      small
      label={t("svc.actions", { name: s.name })}
      items={[
        ...(s.status === "running" && s.port ? [{ label: t("svc.menu.access"), icon: "share" as const, onSelect: () => setShare(true) }] : []),
        { label: t("svc.menu.log"), icon: "file", onSelect: logs },
        ...(openUrl ? [{ label: t("svc.menu.copyaddr"), icon: "copy" as const, onSelect: () => copy(openUrl, "address") }] : []),
        "-" as const,
        ...(s.status === "running" ? [{ label: t("common.stop"), icon: "stop" as const, onSelect: stop }] : []),
        { label: t(s.status === "running" ? "svc.menu.remove.running" : "svc.menu.remove"), icon: "trash", danger: true, onSelect: remove },
      ]}
    />
  );
  return (
    <div className={`service-row ${s.status} ${card ? "erow service" : ""}`}>
      <div className="service-line">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="service-name">
            {s.name}
            {s.port && <span className="chip mono port">:{s.port}</span>}
            {shared && <span className={`chip ${mode === "public" ? "bad" : "attn"}`}>{accessWord(mode)}</span>}
          </div>
          {/* The dot leads the line it describes. Centred beside the whole block, it sat level with
              an access badge whenever a phone wrapped the name, and read as that badge's colour. */}
          <div className="sub service-meta">
            <span className={`dot ${s.status}`} />
            {s.status === "running" ? t("svc.running", { t: relTime(s.started_at) }) : `${statusWord(s.status === "dead" ? "dead" : "stopped")}${s.note ? `: ${s.note}` : ""}${s.stopped_at ? ` · ${relTime(s.stopped_at)}` : ""}`}
            {s.status === "running" && lan && !shared ? ` · ${lan}` : ""}
          </div>
          <div className="sub mono service-cmd" title={s.command}>{s.command}</div>
        </div>
        {openUrl && s.status === "running" && (
          <a className="btn small open" href={openUrl} target="_blank" rel="noreferrer" title={t(shared ? "svc.open.shared" : "svc.open.lan")}>
            {t("common.open")}
          </a>
        )}
        {menu}
      </div>
      {share && s.status === "running" && (
        <Sheet title={t("svc.sheet.title", { name: s.name })} onClose={() => setShare(false)} size="narrow">
          <div className="access-options" role="radiogroup">
            {(["local", "key", "public"] as ShareMode[]).map((m) => (
              <label key={m} className={`access-option ${mode === m ? "on" : ""}`}>
                <input type="radio" name={`access-${s.name}`} checked={mode === m} disabled={busy} onChange={() => setMode(m)} />
                <span>
                  <b>{accessWord(m)}</b>
                  <span className="sub">{m === "local" ? t("svc.access.local.sub", { addr: lan ?? t("svc.access.local.addr") }) : m === "key" ? t("svc.access.key.sub") : t("svc.access.public.sub")}</span>
                </span>
              </label>
            ))}
          </div>
          {!s.share?.public_base && mode !== "local" && <div className="sub" style={{ color: "var(--warn)", margin: "8px 0" }}>{t("svc.nopublic")}</div>}
          {mode !== "local" && s.share?.url && (
            <>
              <label className="field">{t("svc.link")}</label>
              <div className="share-field">
                <input className="field mono" readOnly value={s.share.url} onFocus={(e) => e.target.select()} aria-label={t("svc.sharelink")} />
                <button className="btn small" onClick={() => copy(s.share!.url!, "link")}><Icon name="copy" size={13} /> {t("common.copy")}</button>
              </div>
              {mode === "key" && s.share.key && (
                <>
                  <label className="field">{t("svc.key")}</label>
                  <div className="share-field">
                    <input className="field mono" readOnly value={s.share.key} onFocus={(e) => e.target.select()} aria-label={t("svc.sharekey")} />
                    <button className="btn small" onClick={() => copy(s.share!.key!, "key")}><Icon name="copy" size={13} /> {t("common.copy")}</button>
                    <button className="btn small" disabled={busy} onClick={() => setMode("key", true)} title={t("svc.newkey.title")}>{t("svc.newkey")}</button>
                  </div>
                </>
              )}
              <div className="sub" style={{ marginTop: 8 }}>{t("svc.prefix", { slug: s.share.slug ?? "" })}</div>
            </>
          )}
        </Sheet>
      )}
    </div>
  );
}
