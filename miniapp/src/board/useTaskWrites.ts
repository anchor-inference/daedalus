// The operator's plain edits of a board card: a move, a checklist item, the priority, the due date,
// who does it. Each is one PUT with the card's revision and an operation id that a retry of the same
// edit reuses, so a request the host received but whose answer was lost is not applied twice. A
// refusal is kept with the control that asked, so it is read where the operator looked, not in a
// toast that a drag across the board had already carried the eye away from.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline } from "../store";
import { errorText } from "../ui";
import type { Refusal } from "./BoardColumns";
import type { BoardTask, Status } from "./tasks";

export type TaskEdit = { priority?: number; due_at?: string; assignee_operator?: boolean; assignee_staff_id?: string; status?: Status; check_ids?: string[]; uncheck_ids?: string[] };

export type TaskWrites = {
  /** Writes cannot be confirmed: offline, or the card came from a host without revisions. */
  blocked: (task: BoardTask) => boolean;
  refusal: Refusal | null;
  clear: () => void;
  busy: string | null;
  edit: (task: BoardTask, fields: TaskEdit, where: string) => Promise<boolean>;
  move: (task: BoardTask, status: Status, where: string) => Promise<boolean>;
  check: (task: BoardTask, index: number) => Promise<boolean>;
};

export function useTaskWrites(onChanged: () => void): TaskWrites {
  const offline = useOffline();
  const pending = useRef<{ fingerprint: string; id: string } | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const operationId = (fingerprint: string) => {
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: crypto.randomUUID() };
    return pending.current.id;
  };
  const blocked = (task: BoardTask) => offline || !Number.isInteger(task.entity_revision);
  async function edit(task: BoardTask, fields: TaskEdit, where: string): Promise<boolean> {
    if (blocked(task)) { setRefusal({ taskId: task.id, where, text: t("result.block.unconfirmed") }); return false; }
    const body = { ...fields, expected_entity_revision: task.entity_revision };
    setBusy(where);
    try {
      await api.put(`/api/board/${encodeURIComponent(task.id)}`, { ...body, client_operation_id: operationId(`${task.id}:${JSON.stringify(body)}`) });
      pending.current = null;
      setRefusal(null);
      onChanged();
      return true;
    } catch (e) {
      // A conflict is the host's answer, not a lost one: the next try is a new edit of the card as it is now.
      if (e instanceof ApiError && (e.status === 409 || e.status === 422 || e.status === 403)) { pending.current = null; onChanged(); }
      setRefusal({ taskId: task.id, where, text: errorText(e) });
      return false;
    } finally {
      setBusy(null);
    }
  }
  async function check(task: BoardTask, index: number): Promise<boolean> {
    const where = `check:${index}`;
    if (blocked(task)) { setRefusal({ taskId: task.id, where, text: t("result.block.unconfirmed") }); return false; }
    try {
      // The item's id is the contract's, read fresh: the list may have changed since the card was drawn.
      const contract = await api.get<{ checklist: { id: string; text: string }[] }>(`/api/board/${encodeURIComponent(task.id)}/contract`);
      const criterion = contract.checklist[index];
      const item = task.checklist[index];
      if (!criterion || criterion.text !== item.text || (item.id && item.id !== criterion.id)) {
        setRefusal({ taskId: task.id, where, text: t("board.check.stale") });
        onChanged();
        return false;
      }
      return await edit(task, { [item.done ? "uncheck_ids" : "check_ids"]: [criterion.id] }, where);
    } catch (e) {
      setRefusal({ taskId: task.id, where, text: errorText(e) });
      return false;
    }
  }
  return {
    blocked, refusal, busy, check, edit,
    clear: () => setRefusal(null),
    move: (task, status, where) => edit(task, { status }, where),
  };
}
