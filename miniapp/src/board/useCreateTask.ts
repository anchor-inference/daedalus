// A new card, created the way the host wants a write: against the revision of the board it goes on, and
// with an operation id that a retry of the same words reuses, so a request the host received but whose
// answer was lost is not applied twice. A chat's card goes on its project's board: the board of every
// chat refuses a chat that belongs to a project (403), and every real chat belongs to one, if only the
// scratch project made for it.

import { useRef } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";

/** The fields of a new card as the host's create body takes them, without the revision and the operation id. */
export type NewTask = {
  title: string;
  acceptance?: string;
  priority?: number;
  checklist?: string[];
  brief?: { objective?: string };
  due_at?: string;
  assignee_operator?: boolean;
  session_id: string | null;
  /** The column to make it in; absent, its dependencies decide (To do, or Blocked while one is open). */
  status?: "todo" | "doing" | "blocked";
};

export function useCreateTask(): (projectId: string, task: NewTask) => Promise<void> {
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const revision = useRef<{ key: string; value: number } | null>(null);
  return async function create(projectId, task) {
    const revisions = projectId ? `/api/control/revisions?project=${encodeURIComponent(projectId)}` : "/api/control/revisions";
    if (revision.current?.key !== revisions) {
      const answer = await api.get<{ collection_revision: number }>(revisions);
      if (!Number.isInteger(answer.collection_revision)) throw new Error(t("result.block.unconfirmed"));
      revision.current = { key: revisions, value: answer.collection_revision };
    }
    const body = { ...task, expected_collection_revision: revision.current.value };
    const fingerprint = JSON.stringify(body);
    if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
    try {
      await api.post(projectId ? `/api/projects/${encodeURIComponent(projectId)}/board` : "/api/board", { ...body, client_operation_id: operation.current.id });
      operation.current = null;
      revision.current = null;
    } catch (e) {
      // A conflict is the host's answer, not a lost one: the next try is a new request against the board as it is now.
      if (e instanceof ApiError && e.status === 409) { operation.current = null; revision.current = null; }
      throw e;
    }
  };
}
