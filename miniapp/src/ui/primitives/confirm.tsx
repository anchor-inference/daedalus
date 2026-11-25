// The question asked before something irreversible: the title names the action, the body says what
// it does. Base UI's AlertDialog: it traps focus and cannot be dismissed by a press outside it.

import { AlertDialog } from "@base-ui/react/alert-dialog";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { t } from "../../i18n";
import { closeHandler, useFocusBack, useLayer } from "./layers";
import { STOP } from "./sheet";

/** `choices` asks a three-way question ("Save", "Discard", "Keep editing"): each is a button, and
    the answer is its `value`. Without it the answer is yes or no. */
export type ConfirmOptions = { title: string; body?: ReactNode; action?: string; cancel?: string; danger?: boolean; choices?: ConfirmChoice[] };
export type ConfirmChoice = { label: string; value: string; danger?: boolean };

type Pending = ConfirmOptions & { resolve: (answer: boolean | string) => void };
let pendingSetter: ((f: (cur: Pending | null) => Pending | null) => void) | null = null;

/** Asks before something irreversible. With no host on the page it answers no: there was a
    `window.confirm` here once, and a native prompt over the Telegram webview could not be styled,
    translated or reached by the browser checks. */
export function confirmDialog(opts: ConfirmOptions & { choices?: undefined }): Promise<boolean>;
export function confirmDialog(opts: ConfirmOptions & { choices: ConfirmChoice[] }): Promise<string | false>;
export function confirmDialog(opts: ConfirmOptions): Promise<boolean | string> {
  return new Promise((resolve) => {
    if (!pendingSetter) {
      resolve(false);
      return;
    }
    // A second question while one is open answers the first with "no" rather than leaving it hanging.
    pendingSetter((cur) => {
      cur?.resolve(false);
      return { ...opts, resolve };
    });
  });
}

export function ConfirmHost() {
  const [pending, setPending] = useState<Pending | null>(null);
  useEffect(() => {
    pendingSetter = setPending;
    return () => {
      pendingSetter = null;
    };
  }, []);
  if (!pending) return null;
  return <ConfirmBox key={pending.title} pending={pending} onDone={(answer) => { pending.resolve(answer); setPending((cur) => (cur === pending ? null : cur)); }} />;
}

function ConfirmBox({ pending, onDone }: { pending: Pending; onDone: (answer: boolean | string) => void }) {
  const isTop = useLayer(() => onDone(false));
  const opener = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  useFocusBack(() => opener.current);
  const box = useRef<HTMLDivElement>(null);
  // The safe button takes focus when the action destroys something, the action otherwise.
  const first = () => {
    const buttons = box.current?.querySelectorAll<HTMLButtonElement>(".dialog-actions button") ?? [];
    return (pending.danger ? buttons[0] : buttons[buttons.length - 1]) ?? true;
  };
  return (
    <AlertDialog.Root open onOpenChange={closeHandler(isTop, () => onDone(false))}>
      <AlertDialog.Portal className="overlay-root">
        <AlertDialog.Viewport className="sheet-backdrop confirm" data-overlay-root="" {...STOP}>
          <AlertDialog.Popup ref={box} className="dialog" aria-modal="true" initialFocus={first} finalFocus={false}>
            <AlertDialog.Title render={<h3 id="confirm-title" />}>{pending.title}</AlertDialog.Title>
            {pending.body && <AlertDialog.Description render={<div className="dialog-body" />}>{pending.body}</AlertDialog.Description>}
            <div className="dialog-actions">
              {/* A framed button like its neighbour, not a bare word: beside a filled "Delete task" the
                  way back read as the smaller, lesser choice, and on a touch screen looked like no target. */}
              <button type="button" className="btn" onClick={() => onDone(false)}>{pending.cancel ?? t("common.cancel")}</button>
              {pending.choices
                ? pending.choices.map((choice, i) => (
                  <button key={choice.value} type="button" className={`btn ${choice.danger ? "danger solid" : i === pending.choices!.length - 1 ? "primary" : ""}`} onClick={() => onDone(choice.value)}>{choice.label}</button>
                ))
                : (
                  <button type="button" className={`btn ${pending.danger ? "danger solid" : "primary"}`} onClick={() => onDone(true)}>
                    {pending.action ?? t("common.ok")}
                  </button>
                )}
            </div>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
