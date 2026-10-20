// The approval mode of a conversation in the operator's Chrome, as a chip in the composer: how the
// agent asks there, chosen by the operator for this conversation. The host keeps it; no tool can set it.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { confirmAsync, errorText } from "../ui";
import { Popover } from "../ui/dialogs";

export type ApprovalMode = "manual" | "auto" | "skip";

export const APPROVAL_MODES: { id: ApprovalMode; icon: IconName; label: string; sub: string }[] = [
  { id: "manual", icon: "hand", label: "approvals.manual", sub: "approvals.manual.sub" },
  { id: "auto", icon: "ffwd", label: "approvals.auto", sub: "approvals.auto.sub" },
  { id: "skip", icon: "alert", label: "approvals.skip", sub: "approvals.skip.sub" },
];

/** The chip and its menu, for a conversation the caller knows is in the operator's Chrome (the app
 *  from the conversation's browser choice; the side panel lists no other). A read that fails shows
 *  the default rather than nothing: the composer is not the place to report an unavailable browser.
 *  With no `sessionId` (a conversation about to be made) it holds `value` and reports a pick through
 *  `onPick`; skipping still asks first, and the maker sends the mode with that yes. */
export function ApprovalChip({ sessionId, value, onPick, toast }: { sessionId: string | null; value?: ApprovalMode; onPick?: (mode: ApprovalMode) => void; toast: (text: string) => void }) {
  const [read, setRead] = useState<ApprovalMode | null>(null);
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const path = sessionId ? `/api/sessions/${encodeURIComponent(sessionId)}/browser-approvals` : "";
  const mode = sessionId ? read : value ?? "manual";

  useEffect(() => {
    if (!path) return;
    let gone = false;
    api.get<{ mode?: ApprovalMode }>(path)
      .then((r) => { if (!gone) setRead(r.mode && APPROVAL_MODES.some((m) => m.id === r.mode) ? r.mode : "manual"); })
      .catch(() => { if (!gone) setRead("manual"); });
    return () => { gone = true; };
  }, [path]);

  const confirmSkip = () => confirmAsync(t("approvals.skip.confirm.title"), { body: t("approvals.skip.confirm.body"), action: t("approvals.skip.confirm.action"), danger: true });

  async function choose(next: ApprovalMode) {
    setOpen(false);
    if (next === mode) return;
    if (!path) {
      if (next === "skip" && !(await confirmSkip())) return;
      onPick?.(next);
      return;
    }
    try {
      await api.put(path, { mode: next });
      setRead(next);
    } catch (e) {
      // The first skip on an installation asks once what it means, as the host requires.
      if (next !== "skip" || !(e instanceof ApiError) || e.data.code !== "skip_confirm_needed") return toast(errorText(e));
      if (!(await confirmSkip())) return;
      try {
        await api.put(path, { mode: next, confirm: true });
        setRead(next);
      } catch (again) {
        toast(errorText(again));
      }
    }
  }

  if (!mode) return null;
  const current = APPROVAL_MODES.find((m) => m.id === mode) ?? APPROVAL_MODES[0];
  return (
    <>
      <button ref={button} type="button" className={`approvals-chip ${mode}`} data-mode={mode} aria-haspopup="menu" aria-expanded={open}
        aria-label={t("approvals.label", { mode: t(current.label) })} title={t("approvals.label", { mode: t(current.label) })} onClick={() => setOpen((o) => !o)}>
        <Icon name={current.icon} size={14} />
        <span className="approvals-word">{t(current.label)}</span>
      </button>
      {open && (
        <Popover anchor={button.current} onClose={() => setOpen(false)} className="plus-menu approvals-menu" label={t("approvals.title")}>
          {APPROVAL_MODES.map((m) => (
            <button key={m.id} type="button" role="menuitemradio" aria-checked={m.id === mode} data-mode={m.id} className={m.id === "skip" ? "danger" : undefined} onClick={() => void choose(m.id)}>
              <Icon name={m.icon} size={16} />
              <span className="approvals-item"><b>{t(m.label)}</b><span className="sub">{t(m.sub)}</span></span>
              {m.id === mode && <Icon name="check" size={14} />}
            </button>
          ))}
        </Popover>
      )}
    </>
  );
}
