// The operator's own way to end a card: done or dropped past whatever its review still asks for,
// or deleted from every board. The coordinator's paths all want a proof first, and a card whose
// proof could not be produced used to stay in review with no button that would move it.

import { api } from "../api";
import { t } from "../i18n";
import { errorText } from "../ui";
import { confirmDialog, type MenuItem } from "../ui/dialogs";

type Closable = { id: string; title: string; status: string; entity_revision?: number };
type Hooks = { onDone: () => void; toast: (text: string) => void };

const enc = encodeURIComponent;

/** The dialog with a reason field; null when cancelled. The reason is read from a box the dialog
 *  owns, since the confirmation resolves with a yes or no only. */
async function reasonFor(title: string, body: string, action: string, danger: boolean): Promise<string | null> {
  const typed = { value: "" };
  const ok = await confirmDialog({
    title, action, danger,
    body: <>
      <p>{body}</p>
      <textarea className="field" rows={3} maxLength={2000} aria-label={t("pboard.close.reason")} placeholder={t("pboard.close.reason")}
        onChange={(event) => { typed.value = event.target.value; }} />
    </>,
  });
  return ok ? typed.value.trim() : null;
}

export async function closeCard(task: Closable, status: "done" | "dropped", { onDone, toast }: Hooks): Promise<void> {
  const reason = await reasonFor(t(`pboard.close.${status}.title`, { title: task.title }), t(`pboard.close.${status}.body`),
    t(`pboard.close.${status}.action`).replace(/…$/, ""), status === "dropped");
  if (reason === null) return;
  try {
    await api.post(`/api/board/${enc(task.id)}/close`, {
      status, reason: reason || t("pboard.close.defaultReason"),
      expected_entity_revision: task.entity_revision, client_operation_id: crypto.randomUUID(),
    });
    toast(t(`pboard.close.${status}.saved`));
    onDone();
  } catch (error) {
    toast(errorText(error));
  }
}

export async function removeCard(task: Closable, { onDone, toast }: Hooks): Promise<boolean> {
  const reason = await reasonFor(t("pboard.remove.title", { title: task.title }), t("pboard.remove.body"),
    t("pboard.remove.action").replace(/…$/, ""), true);
  if (reason === null) return false;
  try {
    const query = `client_operation_id=${enc(crypto.randomUUID())}&expected_entity_revision=${task.entity_revision}&reason=${enc(reason)}`;
    await api.delete(`/api/board/${enc(task.id)}?${query}`);
    toast(t("pboard.remove.saved"));
    onDone();
    return true;
  } catch (error) {
    toast(errorText(error));
    return false;
  }
}

/** The menu rows: close as done and drop while the card is open, delete always. */
export function closeItems(task: Closable, disabled: boolean, hooks: Hooks & { onRemoved?: () => void }): MenuItem[] {
  const open = task.status !== "done" && task.status !== "dropped";
  return [
    ...(open ? [
      { label: t("pboard.close.done.action"), icon: "check" as const, disabled, onSelect: () => void closeCard(task, "done", hooks) },
      { label: t("pboard.close.dropped.action"), icon: "archive" as const, disabled, onSelect: () => void closeCard(task, "dropped", hooks) },
    ] : []),
    { label: t("pboard.remove.action"), icon: "trash" as const, danger: true, disabled,
      onSelect: () => void removeCard(task, hooks).then((removed) => { if (removed) hooks.onRemoved?.(); }) },
  ];
}
