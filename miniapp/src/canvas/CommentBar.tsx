// The bar under the board: what the operator tells the agent about this canvas, with the elements and
// cards they pointed at as chips. It writes into the chat that owns the canvas, the one the agent who
// draws on it works in, so a comment lands where the work happens. What goes is shown before it goes,
// exactly as the host will compose it: a picked element carries the page's own markup, and nothing the
// page said reaches the agent without the operator having seen it.

import { useRef, useState } from "react";
import { api, ApiError, type SessionList } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor } from "../router";
import { useQuery } from "../store";
import { errorText } from "../ui";
import { Sheet, useMedia } from "../ui/index";
import { nodeLabel, type Canvas, type CanvasDoc, type CanvasNode } from "./model";
import type { Picked } from "./frames";

export type Chip =
  | ({ id: string; kind: "pick"; frameId: string; page: string; note: string } & Omit<Picked, "rect">)
  | { id: string; kind: "card"; nodeId: string; words: string };

type Pick = { frame_id: string; selector: string; tag: string; text: string; html: string; note: string };

const FENCE = "```";

/** The words of a card chip, for the message and the chip itself. */
export function cardWords(node: CanvasNode): string {
  return nodeLabel(node, node.id).slice(0, 60);
}

/** The text field's part of a message: a line per pointed card, then what was typed. */
export function barText(chips: Chip[], typed: string): string {
  const cards = chips.flatMap((chip) => (chip.kind === "card" ? [`On card ${chip.nodeId} «${chip.words}»:`] : []));
  return [cards.join("\n"), typed.trim()].filter(Boolean).join("\n\n");
}

export function barPicks(chips: Chip[]): Pick[] {
  return chips.flatMap((chip) => (chip.kind === "pick" ? [{ frame_id: chip.frameId, selector: chip.selector, tag: chip.tag, text: chip.text, html: chip.html, note: chip.note }] : []));
}

/** The message as the host composes it (`compose` in api_canvases.py), line for line, so the preview
 *  is what the chat will read. */
export function composeMessage(canvas: { id: string; title: string; version: number; doc: CanvasDoc }, text: string, picks: Pick[]): string {
  const nodes = new Map(canvas.doc.nodes.map((node) => [node.id, node]));
  const lines = [`[Canvas «${canvas.title}» · canvas ${canvas.id} · version ${canvas.version} · /app/canvas/${canvas.id}]`];
  for (const pick of picks) {
    const node = nodes.get(pick.frame_id);
    const where = node ? `${node.file ?? ""}, ${node.width ?? "?"}x${node.height ?? "?"}` : "a frame no longer on the board";
    lines.push(`On frame \`${pick.frame_id}\` (${where}), element \`${pick.selector || pick.tag}\`:`);
    if (pick.html) lines.push(`${FENCE}html\n${pick.html.split(FENCE).join("")}\n${FENCE}`);
    if (pick.note.trim()) lines.push(pick.note.trim());
  }
  if (text.trim()) lines.push(text.trim());
  return lines.join("\n\n");
}

function chipLabel(chip: Chip): string {
  return chip.kind === "pick" ? `${chip.page} · ${chip.selector || chip.tag}` : t("canvas.bar.card", { words: chip.words });
}

export function CommentBar({ canvas, doc, chips, onRemove, onHighlight, onOwner, toast }: {
  canvas: Canvas;
  doc: CanvasDoc;
  chips: Chip[];
  onRemove: (id: string | null) => void;
  onHighlight: (chip: Chip | null) => void;
  onOwner: (sessionId: string) => void;
  toast: (message: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<{ sessionId: string; title: string } | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(true);
  const field = useRef<HTMLTextAreaElement>(null);
  const phone = useMedia("(max-width: 680px)");
  const sessions = useQuery<SessionList>("/api/sessions?view=all", { pollMs: 0, staleMs: 60000 }).data?.sessions;
  const ownerTitle = sessions?.find((session) => session.id === canvas.owner_session_id)?.title || (canvas.scope === "chat" ? canvas.scope_name : "");
  const picks = barPicks(chips);
  const text = barText(chips, typed);
  const preview = composeMessage({ id: canvas.id, title: canvas.title, version: canvas.version, doc }, text, picks);
  const empty = !text.trim() && !picks.length;

  async function send(sessionId?: string) {
    if (empty || busy) return;
    setBusy(true);
    try {
      const answer = await api.post<{ run_id: string; session_id: string; text: string }>(`/api/canvases/${canvas.id}/message`, { text, picks, client_message_id: crypto.randomUUID(), ...(sessionId ? { session_id: sessionId } : {}) });
      if (sessionId) onOwner(answer.session_id);
      setTyped("");
      onRemove(null);
      setChoosing(false);
      const title = sessions?.find((session) => session.id === answer.session_id)?.title || ownerTitle || t("canvas.bar.chat");
      setSent({ sessionId: answer.session_id, title });
      window.setTimeout(() => setSent((current) => (current?.sessionId === answer.session_id ? null : current)), 8000);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && !sessionId && /chat/.test(error.message)) setChoosing(true);
      else toast(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  const candidates = (sessions ?? []).filter((session) => (canvas.scope === "project" ? session.project_id === canvas.scope_id : true)).slice(0, 30);
  return (
    <div className="cv-comment" aria-label={t("canvas.bar")}>
      {chips.length > 0 && (
        <div className="cv-chips" role="list" aria-label={t("canvas.bar.pointed")}>
          {chips.map((chip) => (
            <span key={chip.id} className="cv-chip" role="listitem" tabIndex={0} onMouseEnter={() => onHighlight(chip)} onMouseLeave={() => onHighlight(null)} onFocus={() => onHighlight(chip)} onBlur={() => onHighlight(null)} title={chip.kind === "pick" && chip.note ? chip.note : undefined}>
              <Icon name={chip.kind === "pick" ? "region" : "note"} size={12} />
              <span className="cv-chip-text">{chipLabel(chip)}</span>
              <button type="button" onClick={() => onRemove(chip.id)} aria-label={t("canvas.bar.remove", { what: chipLabel(chip) })} title={t("canvas.bar.remove", { what: chipLabel(chip) })}><Icon name="close" size={12} /></button>
            </span>
          ))}
          <button type="button" className="linkbtn cv-preview-toggle" aria-expanded={previewOpen} onClick={() => setPreviewOpen((open) => !open)}>{t(previewOpen ? "canvas.bar.preview.hide" : "canvas.bar.preview")}</button>
        </div>
      )}
      {chips.length > 0 && previewOpen && <pre className="cv-compose-preview" aria-label={t("canvas.bar.preview")}>{preview}</pre>}
      {sent && (
        <div className="cv-sent" role="status">
          <Icon name="check" size={14} />
          <span>{t("canvas.bar.sent", { chat: sent.title })}</span>
          <button type="button" className="linkbtn" onClick={() => navigate(pathFor("agents", sent.sessionId))}>{t("canvas.bar.open")}</button>
        </div>
      )}
      <form className="cv-comment-row" onSubmit={(event) => { event.preventDefault(); void send(); }}>
        <span className="cv-comment-to" title={ownerTitle ? t("canvas.bar.to", { chat: ownerTitle }) : t("canvas.bar.no.chat")}><Icon name="chat" size={14} /><span>{ownerTitle || t("canvas.bar.no.chat")}</span></span>
        <textarea
          ref={field}
          rows={1}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void send();
            }
          }}
          // A phone has no room for the chat's label, so the field names the chat itself.
          placeholder={phone && ownerTitle ? t("canvas.bar.placeholder.to", { chat: ownerTitle }) : t("canvas.bar.placeholder")}
          aria-label={t("canvas.bar.placeholder")}
        />
        <button type="submit" className="btn primary cv-comment-send" disabled={empty || busy} aria-label={t("canvas.bar.send")} title={t("canvas.bar.send")}><Icon name="send" size={16} /></button>
      </form>
      {choosing && (
        <Sheet title={t("canvas.bar.choose")} onClose={() => setChoosing(false)} size="narrow">
          <p className="cv-choose-body">{t("canvas.bar.choose.body")}</p>
          <div className="cv-choose-list">
            {candidates.length === 0 && <div className="empty">{t("canvas.bar.choose.none")}</div>}
            {candidates.map((session) => (
              <button key={session.id} type="button" className="cv-choose-row" disabled={busy} onClick={() => void send(session.id)}>
                <Icon name="chat" size={15} /><span>{session.title || session.id}</span>
              </button>
            ))}
          </div>
        </Sheet>
      )}
    </div>
  );
}
