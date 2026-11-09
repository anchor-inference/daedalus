// "Allow all from <server>": a span of one session in which every tool of one MCP server runs
// without asking. The approval cards (the app's dock, the Chrome side panel) offer it next to "Allow
// once", the session's MCP panel offers it per server, and the session's settings list what stands
// with the time it has left. Six hours is the default the operator chose; "forever" (until the
// session ends) asks once more, because nothing ends it but the operator.

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { confirmAsync, errorText } from "./ui";

export const SERVER_HOURS = [1, 2, 6, 12, 0] as const;
export const SERVER_DEFAULT_HOURS = 6;

export function spanLabel(hours: number): string {
  return hours === 0 ? t("grant.span.forever") : t("grant.span.hours", { n: hours });
}

/** Ask before a grant with no end; true when the operator still wants it. */
export async function confirmSpan(server: string, hours: number): Promise<boolean> {
  if (hours !== 0) return true;
  return confirmAsync(t("grant.forever.title", { server }), { body: t("grant.forever.body"), action: t("grant.forever.action"), danger: true });
}

/** The button and its span, drawn in the card's own button style. */
export function ServerGrantButton({ server, onGrant, phone = false }: { server: string; onGrant: (hours: number) => void; phone?: boolean }) {
  const [hours, setHours] = useState<number>(SERVER_DEFAULT_HOURS);
  return (
    <span className={`srv-grant${phone ? " phone" : ""}`} data-action="allow-server">
      <button type="button" className={phone ? "ph-btn" : "btn small"} title={t("grant.server.title", { server })}
        onClick={() => void confirmSpan(server, hours).then((yes) => yes && onGrant(hours))}>
        <span className="truncate">{t("grant.server", { server })}</span>
      </button>
      <select className="srv-grant-span" value={hours} aria-label={t("grant.span")} data-forever={hours === 0 ? "" : undefined}
        onKeyDown={(e) => e.stopPropagation()}
        onChange={(e) => setHours(Number(e.target.value))}>
        {SERVER_HOURS.map((h) => <option key={h} value={h}>{spanLabel(h)}{h === 0 ? " ⚠" : ""}</option>)}
      </select>
    </span>
  );
}

export type Standing = {
  id: string; kind: string; label: string; server: string | null; tool: string | null;
  until: number | null; left_seconds: number | null;
};

export function leftLabel(seconds: number | null): string {
  if (seconds === null) return t("grant.left.forever");
  if (seconds < 60) return t("grant.left.soon");
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? t("grant.left.minutes", { n: minutes }) : t("grant.left.hours", { h: Math.floor(minutes / 60), m: minutes % 60 });
}

/** The session's standing grants with the time each has left, each one revocable. */
export function StandingGrants({ sessionId, toast, refresh = 0 }: { sessionId: string; toast: (text: string) => void; refresh?: number }) {
  const [rows, setRows] = useState<Standing[] | null>(null);
  const load = useCallback(() => {
    api.get<{ standing: Standing[] }>(`/api/sessions/${sessionId}/policy/standing`).then((r) => setRows(Array.isArray(r?.standing) ? r.standing : [])).catch(() => setRows([]));
  }, [sessionId]);
  useEffect(load, [load, refresh]);
  // The time left is counted down here between loads; a grant that ran out drops at the next load.
  useEffect(() => {
    const timer = window.setInterval(load, 60_000);
    return () => window.clearInterval(timer);
  }, [load]);
  async function revoke(row: Standing) {
    try {
      const answer = await api.delete<{ standing: Standing[] }>(`/api/sessions/${sessionId}/policy/standing/${row.id}`);
      setRows(Array.isArray(answer?.standing) ? answer.standing : []);
    } catch (error) {
      toast(errorText(error));
    }
  }
  if (rows === null) return null;
  if (rows.length === 0) return <div className="dt-hint quiet" data-grants-empty>{t("grant.none")}</div>;
  return (
    <div className="dt-grants" data-grants>
      {rows.map((row) => (
        <div key={row.id} className="dt-grant" data-grant={row.kind} data-forever={row.left_seconds === null ? "" : undefined}>
          <span className="grow truncate mono" title={row.label}>{row.server ? t("grant.row.server", { server: row.server }) : row.label}</span>
          <span className="dt-grant-left" data-tone={row.left_seconds === null ? "warn" : ""}>
            {row.left_seconds === null ? `⚠ ${leftLabel(null)}` : leftLabel(row.left_seconds)}
          </span>
          <button type="button" className="btn small ghost" onClick={() => void revoke(row)}>{t("grant.revoke")}</button>
        </div>
      ))}
    </div>
  );
}
