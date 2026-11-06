// The form a schedule is made or changed in. The time is chosen in the reader's own clock, as a moment,
// a daily or weekly time or every few hours, and turned into the host's UTC cron line here; a raw cron
// line stays possible for the shapes the choices do not cover.

import { useState } from "react";
import { api, ApiError, type Schedule } from "../api";
import { absTime, cronFor, describeCron, zoneOffsetMinutes } from "../format";
import { t } from "../i18n";
import { approvalExpiry } from "../schedulecard";
import { Sheet } from "../ui/dialogs";
import { errorText } from "../ui";
import { reloadSchedules, type ScheduleEdit } from "./actions";
import type { ScheduleRow } from "./model";

type When = "once" | "daily" | "weekdays" | "weekly" | "hours" | "cron";
/** Monday first, the way a week is picked here; the cron field counts from Sunday. */
const DAYS = [1, 2, 3, 4, 5, 6, 0];

export type LocalRule = { when: When; hour: number; minute: number; days: number[]; every: number };

/**
 * A UTC cron line read back into the form's own choices in the reader's zone, so an existing daily
 * schedule opens on "Daily" at its local time instead of on a raw cron line the operator must decode.
 * ``days`` are the form's Monday-first indices. A shape the choices cannot hold opens as cron.
 */
export function parseLocalCron(cron: string, offsetMinutes = zoneOffsetMinutes()): LocalRule {
  const raw: LocalRule = { when: "cron", hour: 9, minute: 0, days: [0, 2, 4], every: 2 };
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5 || f[2] !== "*" || f[3] !== "*" || !/^\d{1,2}$/.test(f[0])) return raw;
  const minute = Number(f[0]);
  const step = /^\*\/(\d+)$/.exec(f[1]);
  if (step && f[4] === "*") return { ...raw, when: "hours", minute, hour: 0, every: Number(step[1]) };
  if (!/^\d{1,2}$/.test(f[1])) return raw;
  let total = Number(f[1]) * 60 + minute + offsetMinutes;
  let shift = 0;
  while (total < 0) { total += 1440; shift -= 1; }
  while (total >= 1440) { total -= 1440; shift += 1; }
  const local = { ...raw, hour: Math.floor(total / 60), minute: total % 60 };
  if (f[4] === "*") return { ...local, when: "daily" };
  const days: number[] = [];
  for (const part of f[4].split(",")) {
    const m = /^(\d)(?:-(\d))?$/.exec(part);
    if (!m) return raw;
    const a = Number(m[1]) % 7;
    const b = m[2] === undefined ? a : Number(m[2]) % 7;
    if (b < a) return raw;
    for (let d = a; d <= b; d++) days.push((((d + shift) % 7) + 7) % 7);
  }
  const indices = [...new Set(days.map((d) => (d + 6) % 7))].sort();
  if (indices.join(",") === "0,1,2,3,4") return { ...local, when: "weekdays", days: indices };
  return { ...local, when: "weekly", days: indices };
}

const clock = (h: number, m: number) => `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;

/**
 * The fields, inline. ``existing`` edits that schedule and hands the change to ``onSave``; without it
 * the form makes a new one itself, with the approval horizon the host named.
 */
export function ScheduleFormBody({ existing, onCancel, onSave, onCreated, toast, approvalDays }: {
  existing?: ScheduleRow;
  onCancel: () => void;
  onSave?: (edit: ScheduleEdit) => Promise<boolean | undefined>;
  onCreated?: () => void;
  toast: (text: string) => void;
  approvalDays?: number;
}) {
  const rule = existing?.cron ? parseLocalCron(existing.cron) : null;
  const start = existing?.run_at ? new Date(existing.run_at) : new Date(Date.now() + 3600000);
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<Schedule["kind"]>(existing?.kind ?? "agent");
  const [prompt, setPrompt] = useState(existing?.prompt ?? "");
  const [when, setWhen] = useState<When>(rule?.when ?? "once");
  const [date, setDate] = useState(() => start.toLocaleDateString("en-CA"));
  const [time, setTime] = useState(() => (rule && rule.when !== "cron" ? clock(rule.hour, rule.minute) : start.toTimeString().slice(0, 5)));
  const [days, setDays] = useState<number[]>(rule?.days ?? [0, 2, 4]);
  const [every, setEvery] = useState(String(rule?.every ?? 2));
  const [cron, setCron] = useState(existing?.cron ?? "");
  const [busy, setBusy] = useState(false);
  const [created] = useState<{ id: string | null; fingerprint: string }>(() => ({ id: null, fingerprint: "" }));
  const [h, m] = time.split(":").map(Number);

  const built = (() => {
    if (when === "once") {
      const d = new Date(`${date}T${time}`);
      return { run_at: Number.isNaN(d.getTime()) ? null : d.toISOString(), cron: null };
    }
    if (when === "cron") return { run_at: null, cron: cron.trim() || null };
    if (when === "weekly") return { run_at: null, cron: cronFor("weekly", h, m, days.map((d) => DAYS[d])) };
    if (when === "hours") return { run_at: null, cron: cronFor("hours", h, m, [], Number(every) || 1) };
    return { run_at: null, cron: cronFor(when, h, m) };
  })();
  const preview = built.cron ? describeCron(built.cron) : built.run_at ? t("fmt.once", { when: absTime(built.run_at) }) : "";
  const valid = !!(name.trim() && prompt.trim() && (built.cron || built.run_at) && (when !== "weekly" || days.length > 0));

  async function submit() {
    const fields = { name: name.trim(), prompt: prompt.trim(), cron: built.cron, run_at: built.run_at };
    setBusy(true);
    try {
      await api.post("/api/recurring/preview", { ...fields, kind, project_id: existing?.project_id ?? null, target_session: existing?.target_session ?? null });
      if (existing) {
        if (await onSave?.(fields)) onCancel();
        return;
      }
      const fingerprint = JSON.stringify({ ...fields, kind });
      // One identity per request typed: Create pressed again after a lost answer is the same command.
      if (created.fingerprint !== fingerprint) Object.assign(created, { fingerprint, id: crypto.randomUUID() });
      const revision = (await api.get<{ global_collection_revision: number | null }>("/api/recurring/overview")).global_collection_revision;
      if (!revision) throw new Error(t("sched.approval.refresh"));
      await api.post("/api/recurring", {
        ...fields, kind, project_id: null, target_session: null, expected_collection_revision: revision,
        expires_at: approvalExpiry({ cron: built.cron, next_run_at: built.run_at }, Date.now(), approvalDays), client_operation_id: created.id,
      });
      created.fingerprint = "";
      reloadSchedules();
      onCreated?.();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) created.fingerprint = "";
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="schedform">
      <label className="field">{t("common.name")}</label>
      <input className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
      {!existing && (
        <>
          <label className="field">{t("sched.type")}</label>
          <div className="segmented inline" role="radiogroup">
            <button role="radio" aria-checked={kind === "agent"} className={kind === "agent" ? "on" : ""} onClick={() => setKind("agent")}>{t("sched.kind.agent")}</button>
            <button role="radio" aria-checked={kind === "message"} className={kind === "message" ? "on" : ""} onClick={() => setKind("message")}>{t("sched.kind.message")}</button>
          </div>
          <div className="sub schedform-hint">{t(kind === "agent" ? "sched.type.agent.hint" : "sched.type.message.hint")}</div>
        </>
      )}
      <label className="field">{t(kind === "agent" ? "sched.instruction" : "sched.remindertext")}</label>
      <textarea className="field" rows={existing ? 8 : 4} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
      <label className="field">{t("sched.when")}</label>
      {/* Wrapped, not scrolled: in one scrolling row a phone showed four and a half of the six, and
          an existing Cron schedule opened with its own choice out of sight. */}
      <div className="chips wrap">
        {(["once", "daily", "weekdays", "weekly", "hours", "cron"] as When[]).map((w) => (
          <button key={w} className="chip select" aria-pressed={when === w} onClick={() => setWhen(w)}>
            {t(`sched.when.${w}`)}
          </button>
        ))}
      </div>
      {when === "once" && (
        <div className="grid2">
          <div><label className="field">{t("sched.date")}</label><input className="field" type="date" value={date} onChange={(e) => setDate(e.target.value)} /></div>
          <div><label className="field">{t("sched.time")}</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
        </div>
      )}
      {(when === "daily" || when === "weekdays" || when === "weekly" || when === "hours") && (
        <div className="grid2">
          <div><label className="field">{t(when === "hours" ? "sched.minute" : "sched.time")}</label><input className="field" type="time" value={time} onChange={(e) => setTime(e.target.value)} /></div>
          {when === "hours" && <div><label className="field">{t("sched.everyhours")}</label><input className="field" type="number" min={1} max={24} value={every} onChange={(e) => setEvery(e.target.value)} /></div>}
        </div>
      )}
      {when === "weekly" && (
        <>
          <label className="field">{t("sched.days")}</label>
          <div className="chips wrap">
            {DAYS.map((d, i) => (
              <button key={d} className="chip select" aria-pressed={days.includes(i)} onClick={() => setDays((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i].sort()))}>{t(`fmt.dow.${d}`)}</button>
            ))}
          </div>
        </>
      )}
      {when === "cron" && (
        <>
          <label className="field">{t("sched.cron.label")}</label>
          <input className="field mono" placeholder="0 4 * * 1-5" value={cron} onChange={(e) => setCron(e.target.value)} />
        </>
      )}
      <div className="sub preview-line">{preview ? t("sched.preview", { when: preview }) : t("sched.preview.none")}</div>
      <div className="schedform-foot">
        <button className="btn ghost" onClick={onCancel}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !valid} onClick={() => void submit()}>{t(existing ? "common.save" : "common.create")}</button>
      </div>
    </div>
  );
}

/** A new schedule, in a sheet over the list. */
export function NewScheduleSheet({ onClose, toast, approvalDays }: { onClose: () => void; toast: (text: string) => void; approvalDays?: number }) {
  return (
    <Sheet title={t("sched.new")} onClose={onClose}>
      <ScheduleFormBody onCancel={onClose} onCreated={onClose} toast={toast} approvalDays={approvalDays} />
    </Sheet>
  );
}
