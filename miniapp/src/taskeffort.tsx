// How hard a task's model thinks, chosen beside the task's model in Settings.
//
// Every task that names a model (the translator, the review, the heartbeat, the orchestrators…) has an
// effort next to it, stored as "" (the task's default), "none" or one of the reasoning efforts. The
// words are the session effort menu's own, so "low" here is the "low" of the composer. What the default
// is differs: a one-shot helper call does not think, a task that runs a whole agent run keeps its model's
// own effort, and the first row says which, because "default" alone would leave the operator guessing.

import type { ReactNode } from "react";
import { t } from "./i18n";
import { REASONING_EFFORTS } from "./models";
import { SelectionSingle, type SelectionItem } from "./ui/selection";

/** What a task's stored effort may be; "" is the task's default. */
export const TASK_EFFORTS = ["", "none", ...REASONING_EFFORTS] as const;
export type TaskEffort = (typeof TASK_EFFORTS)[number];

/** Which default a task has: no thinking (a helper's call), the model's own (a run), or the Settings one. */
export type EffortDefault = "off" | "model" | { settings: string };

function capital(word: string): string {
  return word ? word[0].toLocaleUpperCase() + word.slice(1) : word;
}

/** The choices after the default: off, then the efforts low to max, each with the composer's hint. */
export function taskEffortItems(): SelectionItem[] {
  return ["none", ...REASONING_EFFORTS].map((value) => ({
    id: value,
    label: capital(t(`add.effort.${value === "none" ? "off" : value}`)),
    detail: t(`composer.effort.${value === "none" ? "off" : value}.hint`),
  }));
}

/** The word a stored effort reads as; "" for the default, which has no word of its own. */
export function taskEffortLabel(value: string): string {
  return taskEffortItems().find((item) => item.id === value)?.label ?? "";
}

/** A default that follows a setting chosen elsewhere: that setting's effort, else the helper's off. */
export function inheritedDefault(effort: string | undefined): EffortDefault {
  return effort ? { settings: taskEffortLabel(effort) } : "off";
}

/** The first row: what "default" comes out as for this task. */
export function effortDefaultLabel(fallback: EffortDefault): string {
  if (fallback === "off") return t("taskeffort.default.off");
  if (fallback === "model") return t("taskeffort.default.model");
  return t("taskeffort.default.settings", { name: fallback.settings });
}

export function TaskEffortPicker({ id, value, onChange, fallback, disabled }: {
  id: string;
  value: string | undefined;
  onChange: (effort: string) => void;
  fallback: EffortDefault;
  disabled?: boolean;
}) {
  return (
    <div className="task-effort" data-effort={value ?? ""}>
      {/* Named once above the picker: a chosen "High" alone, under a model, did not say high what. */}
      <div className="sub task-effort-title">{t("composer.effort.heading")}</div>
      <SelectionSingle id={id} label={t("composer.effort.heading")} items={taskEffortItems()} value={value ?? ""} onChange={onChange}
        defaultOption={{ label: effortDefaultLabel(fallback) }} disabled={disabled} placeholder={t("composer.effort.select")} />
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
