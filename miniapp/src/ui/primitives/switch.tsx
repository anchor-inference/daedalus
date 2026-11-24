// An on/off setting that takes effect at once. It is a switch and not a button labelled "on" or "off",
// because such a button beside an action such as "Run now" read as a second action, and nobody could
// tell whether its word was the state or what pressing it would do. 32x18 on a desktop, 44x26 on a
// phone (controls.css).

import { Switch as Base } from "@base-ui/react/switch";

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (next: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <Base.Root render={<button type="button" />} nativeButton checked={checked} onCheckedChange={(next) => onChange(next)} disabled={disabled}
      aria-label={label} className={`switch ${checked ? "on" : ""}`}>
      <span className="switch-track" aria-hidden="true"><Base.Thumb className="switch-knob" /></span>
    </Base.Root>
  );
}
