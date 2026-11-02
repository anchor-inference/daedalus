// The pieces the task dialog is built from: a text that reads as text until it is edited, the field
// it turns into, a property row, and the panel a rare tool opens in. The dialog used to be one long
// form with every control showing at once; these let it read as the task and edit in place.

import { type ReactNode, type TextareaHTMLAttributes, useLayoutEffect, useRef } from "react";
import { Icon } from "../icons";
import { t } from "../i18n";

/**
 * A textarea as tall as its text. A fixed two-row box cut the brief off mid-sentence, and a box the
 * reader has to drag taller hides the same text behind a handle. The height follows the text on
 * every change and when the dialog's width changes, since a narrower box wraps into more lines.
 */
export function AutoTextarea({ className, value, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const fit = () => {
      el.style.height = "auto";
      // The border is outside scrollHeight; without it the last line sat one border's width short.
      el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
    };
    fit();
    const watcher = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    watcher?.observe(el.parentElement ?? el);
    return () => watcher?.disconnect();
  }, [value]);
  return <textarea ref={box} className={`field auto-grow ${className ?? ""}`} value={value} {...rest} />;
}

/**
 * One part of the brief: its name and its text in full while it is read; a field as tall as the text
 * while it is edited. Edit is a button of its own rather than a click on the text, so selecting a
 * sentence to copy it never turns the text into a field.
 */
export function BriefBlock({ field, label, value, editing, placeholder, onEdit, onChange, disabled }: {
  field: string; label: string; value: string; editing: boolean; placeholder: string;
  onEdit: () => void; onChange: (value: string) => void; disabled?: boolean;
}) {
  return (
    <section className="ptask-brief ptask-block" data-field={field}>
      <div className="ptask-block-head">
        {editing ? <label className="ptask-label" htmlFor={`ptask-${field}`}>{label}</label> : <h4 className="ptask-label">{label}</h4>}
        {!editing && (
          <button type="button" className="iconbtn small ptask-edit" disabled={disabled} onClick={onEdit}
            aria-label={t("ptask.edit.field", { field: label })} title={t("ptask.edit.field", { field: label })}>
            <Icon name="pen" size={14} />
          </button>
        )}
      </div>
      {editing
        ? <AutoTextarea id={`ptask-${field}`} autoFocus maxLength={4000} value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />
        : <div className="ptask-brief-text">{value}</div>}
    </section>
  );
}

/** A property of the task in the side column: its name with the button that changes it, if the
 *  value is not a control already, and the value under them. */
export function Prop({ label, htmlFor, action, children }: { label: string; htmlFor?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <div className="ptask-prop">
      <div className="ptask-prop-head">
        {htmlFor ? <label className="ptask-label" htmlFor={htmlFor}>{label}</label> : <span className="ptask-label">{label}</span>}
        {action}
      </div>
      <div className="ptask-prop-value">{children}</div>
    </div>
  );
}

/**
 * A rare tool, opened from the task's menu, in the place of the task's own text: its name and the way
 * back over it, the tool below. It takes the room the brief had rather than unfolding inside it, so
 * there is never a fold inside a fold and the side column with the task's state stays where it was.
 */
export function ToolPanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <section className="ptask-tool" aria-label={title}>
      <div className="ptask-tool-head">
        <button type="button" className="iconbtn small" onClick={onClose} aria-label={t("ptask.tool.back")} title={t("ptask.tool.back")}>
          <Icon name="back" size={16} />
        </button>
        <h4>{title}</h4>
      </div>
      <div className="ptask-tool-body">{children}</div>
    </section>
  );
}
