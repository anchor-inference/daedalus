// The operator's side of a live link: making one, seeing who uses it, and cutting it off.
//
// A live link is not the read-only page above it in the share dialog. Whoever opens it drives this
// conversation's agent with the operator's own powers, and the dialog says that before anything else.
// Whether the link can be opened from outside at all is said just as plainly: a link on this machine's
// loopback address works for nobody else, and handing one out by mistake is the failure to prevent.

import { useState } from "react";
import { api, ApiError } from "./api";
import { useQuery, invalidate } from "./store";
import { t, plural } from "./i18n";
import { relTime, absTime } from "./format";
import { useCopied } from "./ui/components";
import { confirmAsync, errorText } from "./ui";
import { Icon } from "./icons";
import { Select } from "./ui/primitives";

export type LiveLinkVisitor = { name: string; first_seen_at: string; last_seen_at: string };
export type LiveLink = {
  id: string;
  url: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  active: boolean;
  last_seen_at: string | null;
  connections: number;
  visitors: LiveLinkVisitor[];
};
/** `reach` is who can open a link at all: anyone on the internet, the home network, this machine, or nobody. */
export type LiveLinks = { links: LiveLink[]; reach: "outside" | "network" | "machine" | "none"; base: string };

const EXPIRIES: (number | null)[] = [null, 1, 24, 24 * 7];

export function liveLinksKey(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/live-shares`;
}

export function useLiveLinks(sessionId: string | null): LiveLinks | undefined {
  const data = useQuery<LiveLinks>(sessionId ? liveLinksKey(sessionId) : null, { pollMs: 15000, staleMs: 5000 }).data;
  // A host from before live links answers this route with something else, or not at all: no links then.
  return data && Array.isArray(data.links) ? data : undefined;
}

/** In the conversation's header while a live link is open: that it is, how many have used it, and when. */
export function LiveLinkChip({ sessionId, onOpen }: { sessionId: string; onOpen: () => void }) {
  const data = useLiveLinks(sessionId);
  const active = data?.links.filter((link) => link.active) ?? [];
  if (!active.length) return null;
  const visitors = active.reduce((sum, link) => sum + link.visitors.length, 0);
  const last = active.map((link) => link.last_seen_at).filter(Boolean).sort().at(-1) ?? null;
  const label = [t("live.chip"), plural("live.visitors", visitors), last ? relTime(last) : ""].filter(Boolean).join(" · ");
  return (
    <button type="button" className="chip bad live-chip" onClick={onOpen} title={t("live.chip.title")} data-live-link>
      <Icon name="share" size={12} /> {label}
    </button>
  );
}

/** The live link's part of the share dialog. */
export function LiveLinkSection({ sessionId, toast }: { sessionId: string; toast: (text: string) => void }) {
  const data = useLiveLinks(sessionId);
  const [expiry, setExpiry] = useState<number | null>(24);
  const [busy, setBusy] = useState(false);
  const [copied, copyValue] = useCopied();
  const key = liveLinksKey(sessionId);
  const active = data?.links.filter((link) => link.active) ?? [];
  const ended = data?.links.filter((link) => !link.active).slice(0, 3) ?? [];

  async function create() {
    if (!(await confirmAsync(t("live.confirm.title"), { body: t("live.warning"), action: t("live.create"), danger: true }))) return;
    setBusy(true);
    try {
      await api.post(key, { expires_hours: expiry });
      invalidate(key);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    setBusy(true);
    try {
      await api.delete(`${key}/${encodeURIComponent(id)}`);
      invalidate(key);
      toast(t("live.revoked"));
    } catch (e) {
      toast(e instanceof ApiError && e.status === 404 ? t("live.gone") : errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="live-links" aria-labelledby="live-links-title">
      <h3 id="live-links-title" className="live-title">{t("live.title")}</h3>
      <div className="live-warning" role="note"><Icon name="alert" size={14} /> <span>{t("live.warning")}</span></div>
      {data && <Reach reach={data.reach} />}
      {active.map((link) => (
        <div key={link.id} className="live-link" data-live-active>
          <div className="share-field">
            <input className="field mono" readOnly value={link.url} onFocus={(e) => e.target.select()} aria-label={t("live.url")} />
            <button className="btn small" disabled={!link.url} onClick={async () => { if (!(await copyValue(link.url, link.id))) toast(t("svc.copyfail")); }}>
              <Icon name={copied === link.id ? "check" : "copy"} size={13} /> {t("common.copy")}
            </button>
            <button className="btn small danger" disabled={busy} onClick={() => void revoke(link.id)}>{t("live.revoke")}</button>
          </div>
          <div className="sub live-facts">
            <span>{plural("live.visitors", link.visitors.length)}</span>
            <span>{link.last_seen_at ? t("live.lastseen", { when: relTime(link.last_seen_at) }) : t("live.unused")}</span>
            <span>{link.expires_at ? t("live.expires", { when: absTime(link.expires_at) }) : t("live.noexpiry")}</span>
            {link.connections > 0 && <span>{plural("live.connections", link.connections)}</span>}
          </div>
          {link.visitors.length > 0 && (
            <ul className="live-visitors">
              {link.visitors.slice(0, 6).map((visitor, at) => (
                <li key={at}>{visitor.name || t("live.anonymous")} · {relTime(visitor.last_seen_at)}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
      <div className="live-create">
        <label className="sub" htmlFor="live-expiry">{t("live.expiry")}</label>
        <Select id="live-expiry" size="md" label={t("live.expiry")} value={expiry === null ? "never" : String(expiry)} onChange={(id) => setExpiry(id === "never" ? null : Number(id))}
          options={EXPIRIES.map((hours) => ({ id: hours === null ? "never" : String(hours), label: t(hours === null ? "live.expiry.never" : hours === 1 ? "live.expiry.hour" : hours === 24 ? "live.expiry.day" : "live.expiry.week") }))} />
        <button className="btn small primary" disabled={busy} onClick={() => void create()}>{t(active.length ? "live.create.another" : "live.create")}</button>
      </div>
      {ended.length > 0 && (
        <div className="sub live-ended">
          {ended.map((link) => (
            <div key={link.id}>{t(link.revoked_at ? "live.ended.revoked" : "live.ended.expired", { when: absTime(link.revoked_at ?? link.expires_at) })} · {plural("live.visitors", link.visitors.length)}</div>
          ))}
        </div>
      )}
    </section>
  );
}

function Reach({ reach }: { reach: LiveLinks["reach"] }) {
  const good = reach === "outside";
  return (
    <div className={`live-reach ${good ? "ok" : "warn"}`} data-reach={reach} role="status">
      <Icon name={good ? "check" : "alert"} size={14} />
      <span><b>{t(`live.reach.${reach}`)}</b> {t(`live.reach.${reach}.sub`)}</span>
    </div>
  );
}
