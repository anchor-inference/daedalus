import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Icon } from "../icons";

/** A value that opens choices in place. The caret has its own space even when the value truncates. */
export const ControlTrigger = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode }>(function ControlTrigger({ children, className = "", ...props }, ref) {
  return <button {...props} ref={ref} type="button" className={`control-trigger ${className}`}>
    {children}
    <Icon name="chevron" size={14} />
  </button>;
});
