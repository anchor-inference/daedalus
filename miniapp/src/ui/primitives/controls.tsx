// The controls every screen draws: buttons, icon buttons, a field with its label. Plain markup over
// the `.btn`, `.iconbtn` and `.field` classes the stylesheets and the browser checks already know, so
// a screen moving onto them changes no selector.

import { useId, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from "react";
import { Icon, type IconName } from "../../icons";
import { navigate } from "../../router";
import { Tooltip } from "./tooltip";

export type ButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> & {
  /** The one primary action of a view (violet), an ordinary one, a quiet one without a frame, or one
      that destroys something. */
  variant?: "primary" | "default" | "quiet" | "danger";
  /** 32 or 28 px tall. */
  size?: "md" | "sm";
  icon?: IconName;
  kbd?: string;
  /** Working: the button stays where it is, says so, and takes no second press. */
  busy?: boolean;
  type?: "button" | "submit";
};

const VARIANT: Record<NonNullable<ButtonProps["variant"]>, string> = { primary: "primary", default: "", quiet: "ghost", danger: "danger" };

export function Button({ variant = "default", size = "md", icon, kbd, busy, className = "", children, disabled, type = "button", ...rest }: ButtonProps) {
  const cls = ["btn", VARIANT[variant], size === "sm" ? "small" : "", busy ? "busy" : "", className].filter(Boolean).join(" ");
  return (
    <button {...rest} type={type} className={cls} disabled={disabled || busy} aria-busy={busy || undefined}>
      {busy ? <span className="spinner" aria-hidden="true" /> : icon && <Icon name={icon} size={16} />}
      {children}
      {kbd && <kbd className="kbd btn-kbd">{kbd}</kbd>}
    </button>
  );
}

export type IconButtonProps = {
  icon: IconName;
  label: string;
  /** 32 or 28 px square; a phone's touch size comes from the stylesheet. */
  size?: 32 | 28;
  pressed?: boolean;
  /** A count on the corner: unread, waiting. */
  badge?: ReactNode;
  /** A dot on the corner: something waiting for the operator behind this control. */
  pip?: boolean;
  onClick?: () => void;
  /** A link drawn as an icon button; a plain press navigates inside the app. */
  href?: string;
  disabled?: boolean;
  expanded?: boolean;
  popup?: "menu" | "dialog";
  className?: string;
  /** Left out, the name shows as a tooltip. */
  tooltip?: boolean;
  kbd?: string;
};

/** A glyph button with its name as a tooltip: every icon-only control has a name a pointer can read. */
export function IconButton({ icon, label, size = 32, pressed, badge, pip, onClick, href, disabled, expanded, popup, className = "", tooltip = true, kbd }: IconButtonProps) {
  const cls = ["iconbtn", size === 28 ? "small" : "", pressed ? "on" : "", className].filter(Boolean).join(" ");
  const inner = (
    <>
      <Icon name={icon} size={16} />
      {badge !== undefined && badge !== null && badge !== 0 && badge !== false && <span className="iconbtn-badge" aria-hidden="true">{typeof badge === "number" && badge > 99 ? "99+" : badge}</span>}
      {pip && <span className="iconbtn-pip" aria-hidden="true" />}
    </>
  );
  const control = href ? (
    <a className={cls} href={href} aria-label={label} onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); if (onClick) onClick(); else navigate(href); }}>
      {inner}
    </a>
  ) : (
    <button type="button" className={cls} aria-label={label} onClick={onClick} disabled={disabled} aria-pressed={pressed} aria-expanded={expanded} aria-haspopup={popup}>
      {inner}
    </button>
  );
  return tooltip ? <Tooltip label={label} kbd={kbd}>{control}</Tooltip> : control;
}

type FieldBase = { label: ReactNode; hint?: ReactNode; error?: ReactNode; mono?: boolean };
export type FieldProps = FieldBase & ({ multiline?: false } & InputHTMLAttributes<HTMLInputElement> | { multiline: true } & TextareaHTMLAttributes<HTMLTextAreaElement>);

/** A text field with its label above it and, under it, either a hint or what is wrong. */
export function Field(props: FieldProps) {
  const own = useId();
  const { label, hint, error, mono, multiline, className = "", id = own, ...rest } = props as FieldBase & { multiline?: boolean; className?: string; id?: string } & Record<string, unknown>;
  const note = `${id}-note`;
  const cls = ["field", mono ? "mono" : "", className].filter(Boolean).join(" ");
  const common = { id, className: cls, "aria-invalid": error ? true : undefined, "aria-describedby": hint || error ? note : undefined };
  return (
    <div className="field-box">
      <label className="field-label" htmlFor={id}>{label}</label>
      {multiline ? <textarea {...(rest as TextareaHTMLAttributes<HTMLTextAreaElement>)} {...common} /> : <input {...(rest as InputHTMLAttributes<HTMLInputElement>)} {...common} />}
      {(error || hint) && <div id={note} className={error ? "field-error" : "sub field-hint"}>{error ?? hint}</div>}
    </div>
  );
}
