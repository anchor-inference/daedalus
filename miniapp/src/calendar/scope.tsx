// The question every change to a recurring event has to ask: this one, or the whole series. Asked as
// a dialog the change waits on, so a drag, a resize, an edit and a delete all ask it the same way.

import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../i18n";
import { Overlay, useLayer } from "../ui/dialogs";
import type { Scope } from "./types";

type Ask = { action: "change" | "delete"; resolve: (scope: Scope | null) => void };

export function useScopeQuestion(): [React.ReactNode, (action: Ask["action"]) => Promise<Scope | null>] {
  const [pending, setPending] = useState<Ask | null>(null);
  const ask = useCallback((action: Ask["action"]) => {
    return new Promise<Scope | null>((resolve) => {
      setPending((current) => {
        current?.resolve(null);
        return { action, resolve };
      });
    });
  }, []);
  const element = pending ? <ScopeDialog action={pending.action} onDone={(scope) => { pending.resolve(scope); setPending(null); }} /> : null;
  return [element, ask];
}

function ScopeDialog({ action, onDone }: { action: Ask["action"]; onDone: (scope: Scope | null) => void }) {
  const box = useRef<HTMLDivElement>(null);
  useLayer(() => onDone(null));
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    box.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => opener?.focus?.();
  }, []);
  const trap = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab") return;
    const buttons = Array.from(box.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    buttons[(i + (e.shiftKey ? -1 : 1) + buttons.length) % buttons.length]?.focus();
    e.preventDefault();
  };
  return (
    <Overlay>
      <div className="sheet-backdrop confirm" onClick={() => onDone(null)}>
        <div ref={box} className="dialog cal-scope" role="alertdialog" aria-modal="true" aria-labelledby="cal-scope-title" onClick={(e) => e.stopPropagation()} onKeyDown={trap}>
          <h3 id="cal-scope-title">{t(action === "delete" ? "cal.scope.delete" : "cal.scope.change")}</h3>
          <div className="dialog-body">{t("cal.scope.body")}</div>
          <div className="cal-scope-actions">
            <button type="button" className={`btn ${action === "delete" ? "danger" : "primary"}`} onClick={() => onDone("this")}>{t("cal.scope.this")}</button>
            <button type="button" className={`btn ${action === "delete" ? "danger" : ""}`} onClick={() => onDone("all")}>{t("cal.scope.all")}</button>
            <button type="button" className="btn ghost" onClick={() => onDone(null)}>{t("common.cancel")}</button>
          </div>
        </div>
      </div>
    </Overlay>
  );
}
