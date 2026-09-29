// The one shape a setting takes in Settings: what it is and one grey line about it on the left,
// exactly one control on the right, and a hairline between it and the next.
//
// Settings grew four ways of drawing one value — a button whose label was its own state, a
// full-width select under a label, a number field under a label, and this row in two places — and a
// page that mixed them read as a form rather than as a list of settings. Every scalar setting is now
// one of these rows; only free text that needs the width (addresses, keys, lists, the rules) keeps
// its label above a full-width field.

import type React from "react";
import { useEffect, useState } from "react";
import { numInput } from "./ui";

/** A setting: title and description left, the control right.
 *
 *  `stack` is for a control that cannot be compact — a number with its unit, a long model name — and
 *  lets it take a line of its own under the words on a phone, where beside them it would squeeze the
 *  title to a word a line. A switch or a short choice stays beside the words at every width. */
export function Row({ title, desc, children, stack, htmlFor, className = "", ...data }: {
  title: React.ReactNode;
  desc?: React.ReactNode;
  children?: React.ReactNode;
  stack?: boolean;
  htmlFor?: string;
  className?: string;
  [data: `data-${string}`]: string | undefined;
}) {
  return (
    <div className={`settings-row ${stack ? "stack" : ""} ${className}`.replace(/\s+/g, " ").trim()} {...data}>
      <div className="settings-row-text">
        {htmlFor ? <label className="settings-row-title" htmlFor={htmlFor}>{title}</label> : <div className="settings-row-title">{title}</div>}
        {desc && <div className="settings-row-desc">{desc}</div>}
      </div>
      {children !== undefined && <div className="settings-row-ctl">{children}</div>}
    </div>
  );
}

/** A number typed in place, saved when the field is left or Enter is pressed, with its unit after it.
 *  A value that is not a number, or is under `min`, is put back to what was saved rather than sent. */
export function NumInput({ id, value, min, max, step, unit, label, onSave, invalid }: {
  id?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number | string;
  unit?: string;
  label: string;
  onSave: (v: number) => void;
  invalid?: boolean;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  function commit() {
    const v = numInput(draft, min);
    if (v === null || (max !== undefined && v > max)) {
      setDraft(String(value));
      return;
    }
    if (v !== value) onSave(v);
  }
  return (
    <span className="settings-num">
      <input
        id={id}
        className="field"
        type="number"
        inputMode="decimal"
        min={min}
        max={max}
        step={step}
        value={draft}
        aria-label={label}
        aria-invalid={invalid || undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {/* Always there, empty or not: the units' slot keeps every number field on the page in one
          column, where fields with and without a unit otherwise ended at different edges. */}
      <span className="settings-unit">{unit ?? ""}</span>
    </span>
  );
}

/** A number setting as a row: the commonest row there is. */
export function NumRow({ title, desc, unit, ...input }: { title: string; desc?: React.ReactNode; unit?: string } & Omit<Parameters<typeof NumInput>[0], "label" | "unit">) {
  return (
    <Row title={title} desc={desc} htmlFor={input.id} stack>
      <NumInput {...input} unit={unit} label={title} />
    </Row>
  );
}

/** Free text a line long or more, label above and the whole width below: an address, a key, a
 *  list of engines. The one shape that genuinely needs the width, saved when the field is left. */
export function TextBlock({ label, value, placeholder, onSave, hint, id, type }: { label: string; value: string; placeholder?: string; onSave: (v: string) => void; hint?: string; id?: string; type?: string }) {
  return (
    <div className="settings-block">
      <label className="field" htmlFor={id}>{label}</label>
      <input id={id} className="field" type={type} defaultValue={value} placeholder={placeholder} onBlur={(e) => e.target.value.trim() !== value && onSave(e.target.value.trim())} />
      {hint && <div className="sub">{hint}</div>}
    </div>
  );
}
