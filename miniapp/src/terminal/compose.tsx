// The compose line under an agent's terminal in the desktop workspace: one line that sends what is
// written to the staff member as a message, the same message the staff view's composer sends. It goes
// through the host's delivery worker, which types it in only when the CLI's composer is open and no
// permission or dialog is on the screen, so nothing written here can answer a request by accident.
//
// What it adds over typing into the terminal: the CLI's slash commands as hints, a long paste folded
// into a chip instead of a wall of text in one line, and a reply that quotes what is selected in the
// terminal. A plain shell has none (it has the terminal itself), and a phone has its own compose line.

import { useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { api, type StaffSessionView } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import { composerWhen, nowChoice } from "../staff/model";
import { composeMessage, foldsPaste, lineCount, quoteOf, slashHints } from "./composeline";
import { instanceFor } from "./terminals";
import type { PaneContext } from "./workspace";
import "./compose.css";

const enc = encodeURIComponent;

type Paste = { n: number; text: string };

export function AgentCompose({ pane, toast }: { pane: PaneContext; toast: (text: string) => void }) {
  const row = pane.row;
  const id = pane.id ?? "";
  const staffId = row?.owner.id ?? "";
  const name = row?.owner.label || row?.title || "";
  const harness = row?.agent?.kind ?? null;
  const offline = useOffline();
  // What "now" means for this CLI, as the staff view's composer reads it.
  const { data: view } = useQuery<StaffSessionView>(staffId ? `/api/staff/${enc(staffId)}/session` : null, { staleMs: 10000 });
  const [text, setText] = useState("");
  const [pastes, setPastes] = useState<Paste[]>([]);
  const [quote, setQuote] = useState("");
  const [selection, setSelection] = useState("");
  const [pick, setPick] = useState(0);
  const [busy, setBusy] = useState(false);
  const counter = useRef(0);
  const field = useRef<HTMLInputElement>(null);

  // The terminal's selection, for Reply. Subscribed again once the pane has a state: the instance is
  // made by the terminal's own view, which may mount after this line asked for it.
  const opened = !!pane.state;
  useEffect(() => {
    const instance = id ? instanceFor(id) : undefined;
    if (!instance) return;
    const read = () => setSelection(instance.selectionText());
    read();
    return instance.onSelection(read);
  }, [id, opened]);

  const hints = slashHints(harness, text);
  const at = hints ? Math.min(pick, hints.length - 1) : 0;
  const message = composeMessage({ quote, typed: text, pastes: pastes.map((p) => p.text) });
  const held = pane.status === "waiting";

  function complete(command: string) {
    setText(`/${command} `);
    setPick(0);
    field.current?.focus();
  }

  async function send() {
    if (!message || busy || !staffId) return;
    if (offline) {
      toast(t("focus.staffDraftOffline"));
      return;
    }
    // Cleared at once and put back on a failure: the line is for quick words, and a second message
    // typed while the first is on its way must not be wiped by the first one's answer.
    const kept = { text, pastes, quote };
    setText("");
    setPastes([]);
    setQuote("");
    setBusy(true);
    try {
      await api.post(`/api/staff/${enc(staffId)}/messages`, { text: message, when: composerWhen(nowChoice(view?.capabilities), null) });
      toast(t("phone.told", { name }));
      invalidate(`/api/staff/${enc(staffId)}/messages`);
    } catch (e) {
      setText((now) => now || kept.text);
      setPastes((now) => (now.length ? now : kept.pastes));
      setQuote((now) => now || kept.quote);
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.nativeEvent.isComposing) return;
    if (hints && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setPick((at + (e.key === "ArrowDown" ? 1 : hints.length - 1)) % hints.length);
      return;
    }
    if (hints && (e.key === "Tab" || e.key === "Enter")) {
      e.preventDefault();
      complete(hints[at]);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
      return;
    }
    if (e.key === "Escape") {
      // Back to the program, as Escape leaves any field laid over a terminal.
      e.preventDefault();
      instanceFor(id)?.focus();
      return;
    }
    if (e.key === "Backspace" && !text && e.currentTarget.selectionStart === 0) {
      // An empty line takes the last chip back, the way a chip input does.
      if (pastes.length) setPastes((all) => all.slice(0, -1));
      else if (quote) setQuote("");
    }
  }

  function onPaste(e: ClipboardEvent<HTMLInputElement>) {
    const pasted = e.clipboardData.getData("text/plain");
    if (!pasted || !foldsPaste(pasted)) return;
    e.preventDefault();
    counter.current += 1;
    setPastes((all) => [...all, { n: counter.current, text: pasted }]);
  }

  function reply() {
    const quoted = quoteOf(selection);
    if (!quoted) return;
    setQuote(quoted);
    field.current?.focus();
  }

  const placeholder = held ? t("term.compose.placeholder.held", { name }) : t("term.compose.placeholder", { name });
  return (
    <div className="tp-compose" data-compose={id} data-held={held ? "1" : undefined}>
      {hints && (
        <ul className="tp-slash" role="listbox" aria-label={t("term.compose.commands")}>
          {hints.map((command, i) => (
            <li key={command} role="option" aria-selected={i === at} className={`tp-slash-row ${i === at ? "on" : ""}`} data-command={command}
              onMouseDown={(e) => { e.preventDefault(); complete(command); }}>
              <span className="tp-slash-name">/{command}</span>
              <span className="tp-slash-what">{t(`term.compose.cmd.${command}`)}</span>
            </li>
          ))}
        </ul>
      )}
      {quote && (
        <span className="tp-chip quote" data-chip="quote" title={quote}>
          <Icon name="reply" size={12} />
          <span className="tp-chip-text">{quote.split("\n")[0].replace(/^> /, "")}</span>
          <button type="button" className="tp-chip-x" onClick={() => setQuote("")} aria-label={t("term.compose.unquote")} title={t("term.compose.unquote")}><Icon name="close" size={11} /></button>
        </span>
      )}
      {pastes.map((p) => (
        <span key={p.n} className="tp-chip paste" data-chip="paste" title={p.text.split("\n").slice(0, 12).join("\n")}>
          <span className="tp-chip-text">{plural("term.compose.pasted", lineCount(p.text))}</span>
          <button type="button" className="tp-chip-x" onClick={() => setPastes((all) => all.filter((x) => x.n !== p.n))} aria-label={t("term.compose.unpaste")} title={t("term.compose.unpaste")}><Icon name="close" size={11} /></button>
        </span>
      ))}
      <input
        ref={field}
        className="tp-compose-field"
        value={text}
        maxLength={64000}
        placeholder={placeholder}
        aria-label={placeholder}
        title={held ? t("term.compose.held") : undefined}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => { setText(e.target.value); setPick(0); }}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
      />
      {selection.trim() && (
        <button type="button" className="btn small ghost tp-compose-reply" onMouseDown={(e) => e.preventDefault()} onClick={reply} title={t("term.compose.reply.tip")}>
          <Icon name="reply" size={13} />
          <span>{t("term.compose.reply")}</span>
        </button>
      )}
      <button type="button" className="iconbtn small flat tp-compose-send" onClick={() => void send()} disabled={!message || busy || offline} aria-label={t("staff.send")} title={offline ? t("staff.compose.offline") : t("staff.send")}>
        <Icon name="send" size={14} />
      </button>
    </div>
  );
}
