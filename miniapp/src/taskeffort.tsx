// How hard a task's model thinks, chosen beside the task's model.
//
// Every task that names a model (the translator, the review, the heartbeat, the orchestrators…) has an
// effort next to it, stored as "" (the task's default), "none" or one of the reasoning efforts. The
// words are the composer's own (Off, Low … Max), so "Low" here is the "Low" of the model picker. What
// the default is differs: a one-shot helper call does not think, a task that runs a whole agent run
// keeps its model's own effort, and the first choice says which, because "default" alone would leave
// the operator guessing.

import type { ReactNode } from "react";
import { t } from "./i18n";
import { REASONING_EFFORTS } from "./models";
import { Select, type SelectOption } from "./ui/primitives/select";

/** Which default a task has: no thinking (a helper's call), the model's own (a run), or the Settings one. */
export type EffortDefault = "off" | "model" | { settings: string };

/** The choices after the default: off, then the efforts low to max, in the composer's words. */
export function taskEffortItems(): SelectOption<string>[] {
  return ["none", ...REASONING_EFFORTS].map((value) => ({ id: value, label: t(`add.effort.${value === "none" ? "off" : value}`) }));
}

/** The word a stored effort reads as; "" for the default, which has no word of its own. */
export function taskEffortLabel(value: string): string {
  return taskEffortItems().find((item) => item.id === value)?.label ?? "";
}

/** A default that follows a setting chosen elsewhere: that setting's effort, else the helper's off. */
export function inheritedDefault(effort: string | undefined): EffortDefault {
  return effort ? { settings: taskEffortLabel(effort) } : "off";
}

/** The first choice: what "default" comes out as for this task. */
export function effortDefaultLabel(fallback: EffortDefault): string {
  if (fallback === "off") return t("taskeffort.default.off");
  if (fallback === "model") return t("taskeffort.default.model");
  return t("taskeffort.default.settings", { name: fallback.settings });
}

/** The effort as a line of its own: its name, and a compact list of the default and the efforts. */
export function TaskEffortPicker({ id, value, onChange, fallback, disabled }: {
  id: string;
  value: string | undefined;
  onChange: (effort: string) => void;
  fallback: EffortDefault;
  disabled?: boolean;
}) {
  return (
    <div className="task-effort" data-effort={value ?? ""}>
      {/* Named beside the list: a chosen "High" alone, under a model, did not say high what. */}
      <span className="task-effort-title">{t("composer.effort.short")}</span>
      <Select id={id} className="task-effort-value" label={t("composer.effort.short")} value={value ?? ""} onChange={onChange} disabled={disabled}
        options={[{ id: "", label: effortDefaultLabel(fallback) }, ...taskEffortItems()]} />
    </div>
  );
}

/** A task's model picker with its effort under it, as one control of the row. */
export function WithEffort({ children, ...effort }: Parameters<typeof TaskEffortPicker>[0] & { children: ReactNode }) {
  return (
    <div className="task-model">
      {children}
      <TaskEffortPicker {...effort} />
    </div>
  );
}
