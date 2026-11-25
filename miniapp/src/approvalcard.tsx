// The agent waiting on the operator's word, in the composer's own place: one card as wide as the
// composer, what is asked, the command in a well, why it asks, and three answers — refuse (Esc), allow
// similar calls for good (in the project, or in this chat where the project cannot take it), allow
// once (Enter, the primary). It used to be a dock above the field with the field still under it, so
// the page asked two things at once; the draft is kept underneath and comes back with the field.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Approval } from "./composer";
import { Icon } from "./icons";
import { t } from "./i18n";
import { errorText } from "./ui";
import { layerOpen } from "./ui/primitives";
import { ServerGrantButton } from "./servergrant";

/** Whether the host would keep a grant for the whole project, as `GET …/policy/similar/{key}` says. */
export type ProjectScope = { available: boolean; name: string; reason: string };

/** What the host says about the project scope of one request; null until it has answered. */
export function useProjectScope(sessionId: string, key: string, enabled: boolean): ProjectScope | null {
  const [scope, setScope] = useState<{ key: string; scope: ProjectScope } | null>(null);
  useEffect(() => {
    if (!enabled || !key || !sessionId) return;
    let gone = false;
    api.get<{ project?: { available?: unknown; project?: { name?: unknown } | null; reason?: unknown } | null }>(`/api/sessions/${sessionId}/policy/similar/${key}`)
      .then((r) => {
        const p = r?.project;
        if (gone || !p) return;
        setScope({ key, scope: { available: p.available === true, name: typeof p.project?.name === "string" ? p.project.name : "", reason: typeof p.reason === "string" ? p.reason : "" } });
      })
      .catch(() => undefined);
    return () => { gone = true; };
  }, [sessionId, key, enabled]);
  return scope && scope.key === key ? scope.scope : null;
}

export function ApprovalCard({ sessionId, approval, phone, similar, server, onApprove, onApproveSimilar, onApproveServer, onDeny, toast, onAnswered }: {
  sessionId: string;
  approval: Approval;
  phone: boolean;
  /** What "allow similar" grants (``npm test*``), when the host offers a family for the request. */
  similar?: string | null;
  server?: string | null;
  onApprove?: (a: Approval) => void;
  onApproveSimilar?: (a: Approval) => void;
  onApproveServer?: (a: Approval, hours: number) => void;
  onDeny?: (a: Approval) => void;
  toast: (text: string) => void;
  /** The card was answered here: the composer gives the field back its focus on a refusal, and hides a
   *  card the project's rule answered (no handler of the screen's ran, so nothing else would). */
  onAnswered?: (how: "allow" | "deny" | "settled") => void;
}) {
  const card = useRef<HTMLDivElement>(null);
  const kind = approval.chrome ? "chrome" : approval.blocked ? "blocked" : "policy";
  const widen = !!similar && !!onApproveSimilar && !approval.chrome;
  const scope = useProjectScope(sessionId, approval.key, widen);
  const forProject = widen && !!scope?.available;
  const title = approval.chrome ? t("composer.chrome.title", { name: approval.chrome.name })
    : approval.blocked ? t("composer.blocked.title", { tool: approval.tool || "tool" }) : t("composer.approval.title", { tool: approval.tool || "tool" });
  const approveLabel = t(approval.chrome ? "composer.chrome.allow" : approval.blocked ? "composer.allowAnyway" : "composer.approve");
  const denyLabel = t(approval.chrome ? "composer.chrome.deny" : approval.blocked ? "composer.keepBlocked" : "composer.deny");
  const allow = () => { onApprove?.(approval); onAnswered?.("allow"); };
  const deny = () => { onDeny?.(approval); onAnswered?.("deny"); };
  // The project's rule is asked of the host here; a call the project may not take (a force push, a
  // host action) is refused with a reason, and then the chat's own grant is made instead and said so.
  const widenGrant = async () => {
    if (!forProject) { onApproveSimilar?.(approval); onAnswered?.("allow"); return; }
    try {
      await api.post(`/api/sessions/${sessionId}/policy/grant-project`, { key: approval.key });
      onAnswered?.("settled");
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        onApproveSimilar?.(approval);
        onAnswered?.("allow");
        toast(t("composer.approve.project.fallback", { reason: errorText(e) }));
      } else toast(errorText(e));
    }
  };
  // Enter allows once and Escape refuses, as the buttons say, while nothing else has the keyboard: a
  // field being typed in, or a menu or sheet above the card, keeps its own keys.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || layerOpen()) return;
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      if (typing) return;
      // Enter on a focused button presses that button, as it always does.
      if (e.key === "Enter" && target?.tagName !== "BUTTON" && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        allow();
      } else if (e.key === "Escape") {
        e.preventDefault();
        deny();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });
  const widenLabel = forProject ? t("composer.approve.project") : t("composer.approve.similar");
  const widenTitle = forProject ? t("composer.approve.project.title", { label: similar ?? "", project: scope?.name ?? "" }) : t("composer.approve.similar.title", { label: similar ?? "" });
  return (
    <div ref={card} className={`approval-card ${phone ? "phone" : ""}`} role="alertdialog" aria-label={title} data-kind={kind}>
      <div className="approval-eyebrow"><Icon name={approval.blocked ? "shield" : "hand"} size={phone ? 20 : 16} />{t(approval.blocked ? "composer.tray.blocked" : "composer.approval.needed")}</div>
      <div className="approval-question">{title}</div>
      {approval.detail && (approval.chrome
        ? <div className="approval-why">{approval.detail}</div>
        : <div className="approval-command mono" title={approval.detail}>{approval.detail}</div>)}
      {approval.blocked && <div className="approval-why" data-blocked-reason>{t("composer.blocked.reason", { reason: approval.blocked })}</div>}
      {!approval.blocked && approval.reason && <div className="approval-why">{approval.reason.charAt(0).toLocaleUpperCase() + approval.reason.slice(1)}</div>}
      {approval.chrome && <div className="approval-why" data-chrome-consent>{t("uc.switch.consent.body")}</div>}
      <div className="approval-actions">
        <button type="button" className="btn approval-deny" data-action="deny" onClick={deny}>{denyLabel}{!phone && <kbd className="kbd">Esc</kbd>}</button>
        <span className="grow" />
        {server && onApproveServer && !approval.chrome && <ServerGrantButton phone={phone} server={server} onGrant={(hours) => { onApproveServer(approval, hours); onAnswered?.("allow"); }} />}
        {widen && (
          <button type="button" className="btn approval-widen" data-action="allow-similar" data-scope={forProject ? "project" : "chat"} title={widenTitle} onClick={() => void widenGrant()}>
            <span className="truncate">{widenLabel}</span>
          </button>
        )}
        <button type="button" className="btn primary approval-allow" data-action="allow" onClick={allow}>{approveLabel}{!phone && <kbd className="kbd">Enter</kbd>}</button>
      </div>
    </div>
  );
}
