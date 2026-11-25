// One of many, or of a few with long names, picked from a list that opens over the page: a compact
// button that shows the value and a caret, so a settings row keeps its control in the right-hand
// lane. A full-width native select there pushed the value under its label and made every such row
// two lines tall, and a native list could not carry a line saying what each choice means.
//
// Base UI's Select is pinned at 1.9.0 while its issue #5358 is open: on a loaded machine a rapid
// open and close could loop. tests/browser/check_select_stress.py opens and closes one fifty times
// under a four-times CPU throttle and fails on a long task or a list left open.

import { Select as Base } from "@base-ui/react/select";
import { useState } from "react";
import { Icon, type IconName } from "../../icons";
import { closeHandler, useLayer } from "./layers";

export type SelectOption<T extends string> = { id: T; label: string; hint?: string; disabled?: boolean; icon?: IconName };

export type SelectProps<T extends string> = {
  value: T;
  options: SelectOption<T>[];
  onChange: (id: T) => void;
  label: string;
  /** `sm` (28) in a settings row, `md` (32) on its own. */
  size?: "sm" | "md";
  id?: string;
  invalid?: boolean;
  disabled?: boolean;
  className?: string;
};

export function Select<T extends string>({ value, options, onChange, label, size = "sm", id, invalid, disabled, className = "" }: SelectProps<T>) {
  const current = options.find((o) => o.id === value);
  const shown = current?.label ?? "";
  const [open, setOpen] = useState(false);
  // In the stack exactly while the list is open, so Escape closes the list and leaves the settings
  // dialog around it open.
  const isTop = useLayer(() => setOpen(false), open);
  const close = closeHandler(isTop, () => setOpen(false));
  return (
    <span className={`dropdown ${size} ${className}`.trim()}>
      <Base.Root
        value={value}
        onValueChange={(next) => { if (next !== null && next !== value) onChange(next as T); }}
        items={options.map((o) => ({ value: o.id, label: o.label }))}
        disabled={disabled}
        modal={false}
        open={open}
        onOpenChange={(next, details) => (next ? setOpen(true) : close(next, details))}
      >
        <Base.Trigger id={id} className="dropdown-btn" aria-label={`${label}: ${shown}`} aria-invalid={invalid || undefined} data-value={value} title={shown}>
          <Base.Value className="dropdown-value">{() => shown}</Base.Value>
          <Base.Icon className="dropdown-caret" render={<span />}><Icon name="chevron" size={14} /></Base.Icon>
        </Base.Trigger>
        {/* Drawn only while open. Base UI keeps a closed list in the page, transparent: twenty-eight
            selects on a settings page then carried every option of every list, and a check that asked
            for "the option" found one in a list nobody had opened. Typing to a choice on the closed
            button is what that costs. */}
        {open && <SelectList options={options} label={label} />}
      </Base.Root>
    </span>
  );
}

function SelectList<T extends string>({ options, label }: { options: SelectOption<T>[]; label: string }) {
  return (
    <Base.Portal>
      <Base.Positioner className="layer-pos select-pos" alignItemWithTrigger={false} side="bottom" align="end" sideOffset={4} collisionPadding={8}>
        <Base.Popup className="dropdown-list" aria-label={label}>
          {options.map((option) => (
            <Base.Item key={option.id} value={option.id} disabled={option.disabled} className="dropdown-item" data-value={option.id} label={option.label}>
              {option.icon && <Icon name={option.icon} size={16} />}
              <span className="dropdown-item-text">
                <Base.ItemText>{option.label}</Base.ItemText>
                {option.hint && <span className="sub">{option.hint}</span>}
              </span>
              <Base.ItemIndicator className="dropdown-check"><Icon name="check" size={14} /></Base.ItemIndicator>
            </Base.Item>
          ))}
        </Base.Popup>
      </Base.Positioner>
    </Base.Portal>
  );
}
