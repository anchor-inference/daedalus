// The operator's commands on a schedule, each sent the way the host asks for them: with the revision
// the operator saw and one command identity per intent, so a press repeated after a lost answer is
// the same command to the host and cannot run twice.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { approvalExpiry } from "../schedulecard";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { type CycleHistory, proposalScope, type ProposalList, type ScheduleProposal, type ScheduleRow } from "./model";

export type ScheduleEdit = { name: string; prompt: string; cron: string | null; run_at: string | null };

function revisionOf(s: ScheduleRow): number {
  if (!Number.isInteger(s.schedule_revision)) throw new Error(t("sched.approval.refresh"));
  return s.schedule_revision!;
}

export function reloadSchedules(): void {
  invalidate("/api/schedules");
  invalidate("/api/recurring/proposals");
}

/**
 * The commands, and which one is in flight. ``proposals`` is the list the screen already holds: it
 * carries the collection revisions a decision is checked against and how long an Accept lasts.
 */
export function useScheduleActions(toast: (text: string) => void, proposals: ProposalList | undefined) {
  const [busy, setBusy] = useState<string | null>(null);
  const pending = useRef<Record<string, { fingerprint: string; id: string }>>({});
  const days = proposals?.approval_days;
  // Fixed when the screen opens, not at each press: a retried command must send the same body under
  // the same identity, or the host refuses it as a different command.
  const [clock] = useState(() => Date.now());

  const operationId = (key: string, body: unknown) => {
    const fingerprint = JSON.stringify(body);
    if (pending.current[key]?.fingerprint !== fingerprint) pending.current[key] = { fingerprint, id: crypto.randomUUID() };
    return pending.current[key].id;
  };
  const current = async (s: ScheduleRow) => {
    const history = await api.get<CycleHistory>(`/api/recurring/${encodeURIComponent(s.id)}/cycles`);
    if (history.collection_revision == null) throw new Error(t("sched.approval.refresh"));
    return history;
  };

  async function run<T>(key: string, work: () => Promise<T>): Promise<T | undefined> {
    setBusy(key);
    try {
      const result = await work();
      return result;
    } catch (e) {
      // Changed elsewhere first: the next press is a new command on what the host now holds.
      if (e instanceof ApiError && e.status === 409) delete pending.current[key];
      toast(errorText(e));
      return undefined;
    } finally {
      setBusy(null);
      reloadSchedules();
    }
  }

  /** Approve a schedule as it now stands; every change drops its approval, so an edit or a resume ends here. */
  async function approveRevision(s: ScheduleRow, scheduleRevision: number, collectionRevision: number, key: string) {
    const body = { expires_at: approvalExpiry({ cron: s.cron, next_run_at: s.next_run_at ?? s.run_at }, clock, days), expected_schedule_revision: scheduleRevision, expected_collection_revision: collectionRevision };
    await api.post(`/api/recurring/${encodeURIComponent(s.id)}/approve`, { ...body, client_operation_id: operationId(`${key}:approve`, body) });
    delete pending.current[`${key}:approve`];
  }

  return {
    busy,
    approvalDays: days,
    requireApproval: !!proposals?.require_approval,

    runNow: (s: ScheduleRow) => run(`${s.id}:run`, async () => {
      const history = await current(s);
      const body = { expected_schedule_revision: revisionOf(s), expected_collection_revision: history.collection_revision! };
      await api.post(`/api/recurring/${encodeURIComponent(s.id)}/run`, { ...body, client_operation_id: operationId(`${s.id}:run`, body) });
      delete pending.current[`${s.id}:run`];
    }),

    /** Pause, or resume and approve again: a resumed schedule that kept no approval would sit there and never fire. */
    setEnabled: (s: ScheduleRow, enabled: boolean) => run(`${s.id}:enabled`, async () => {
      const history = await current(s);
      const body = { enabled, expected_schedule_revision: revisionOf(s), expected_collection_revision: history.collection_revision! };
      const changed = await api.patch<{ schedule_revision: number; entity_revision: number }>(`/api/recurring/${encodeURIComponent(s.id)}`, { ...body, client_operation_id: operationId(`${s.id}:enabled`, body) });
      delete pending.current[`${s.id}:enabled`];
      if (enabled) await approveRevision(s, changed.schedule_revision, changed.entity_revision, `${s.id}:enabled`);
    }),

    approve: (s: ScheduleRow) => run(`${s.id}:approve`, async () => {
      const history = await current(s);
      await approveRevision(s, history.schedule_revision, history.collection_revision!, s.id);
    }),

    save: (s: ScheduleRow, edit: ScheduleEdit) => run(`${s.id}:save`, async () => {
      const history = await current(s);
      const body = { ...edit, expected_schedule_revision: revisionOf(s), expected_collection_revision: history.collection_revision! };
      const changed = await api.patch<{ schedule_revision: number; entity_revision: number }>(`/api/recurring/${encodeURIComponent(s.id)}`, { ...body, client_operation_id: operationId(`${s.id}:save`, body) });
      delete pending.current[`${s.id}:save`];
      await approveRevision({ ...s, ...edit }, changed.schedule_revision, changed.entity_revision, `${s.id}:save`);
      return true;
    }),

    remove: (s: ScheduleRow) => run(`${s.id}:remove`, async () => {
      const history = await current(s);
      const body = { expected_schedule_revision: revisionOf(s), expected_collection_revision: history.collection_revision! };
      await api.post(`/api/recurring/${encodeURIComponent(s.id)}/remove`, { ...body, client_operation_id: operationId(`${s.id}:remove`, body) });
      delete pending.current[`${s.id}:remove`];
      return true;
    }),

    decide: (p: ScheduleProposal, action: "accept" | "withdraw" | "review-files") => run(`${p.id}:${action}`, async () => {
      const revision = proposals?.collection_revisions[proposalScope(p)];
      if (!Number.isInteger(revision)) throw new Error(t("sched.approval.refresh"));
      const body = { request_digest: p.request_digest, expected_proposal_revision: p.proposal_revision, expected_collection_revision: revision!,
        ...(action === "accept" ? { expires_at: approvalExpiry(p, clock, days) } : {}) };
      const key = `${p.id}:${action}`;
      const answer = await api.post<{ id?: string }>(`/api/recurring/proposals/${encodeURIComponent(p.id)}/${action}`, { ...body, client_operation_id: operationId(key, body) });
      delete pending.current[key];
      return answer;
    }),
  };
}

export type ScheduleActions = ReturnType<typeof useScheduleActions>;
