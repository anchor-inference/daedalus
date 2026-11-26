// One of a small fixed set, picked in place: the one choice control Settings uses, so a mode, an
// approval rule and an on/off setting all look and answer the same way. Base UI's ToggleGroup gives
// it the roving focus (arrows move between the choices, Tab leaves the group); the roles stay a radio
// group's, because that is what one-of-a-few is to a screen reader, and the checks ask for radios.

import { Toggle } from "@base-ui/react/toggle";
import { ToggleGroup } from "@base-ui/react/toggle-group";
import type { ReactNode } from "react";

export type SegmentedOption<T extends string> = { id: T; label: ReactNode; badge?: ReactNode; disabled?: boolean };

export function Segmented<T extends string>({ value, options, onChange, label, size = "sm", className = "" }: { value: T; options: SegmentedOption<T>[]; onChange: (id: T) => void; label?: string; size?: "sm" | "md"; className?: string }) {
  return (
    <ToggleGroup
      value={[value]}
      // A press on the chosen one would empty the group: a one-of-a-few always has one.
      onValueChange={(next) => { const picked = next.find((id) => id !== value); if (picked) onChange(picked as T); }}
      className={`segmented inline ${size} ${className}`.replace(/\s+/g, " ").trim()}
      role="radiogroup"
      aria-label={label}
    >
      {options.map((option) => (
        <Toggle key={option.id} value={option.id} disabled={option.disabled} className={value === option.id ? "on" : ""}
          role="radio" aria-checked={value === option.id} aria-pressed={undefined}>
          <span className="segmented-label">{option.label}</span>
          {option.badge}
        </Toggle>
      ))}
    </ToggleGroup>
  );
}
