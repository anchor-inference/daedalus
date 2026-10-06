// The diff sheet and the result's comment form are siblings in the task sheet, neither owning the
// other. A click on a diff line is handed across here, keyed by task, so the form can prefill its
// file and line without the two components sharing state through their parent.

import { useEffect, useRef } from "react";

export type CommentAnchor = { path: string; line: number };

const listeners = new Map<string, Set<(anchor: CommentAnchor) => void>>();

/** Hand an anchor to the task's comment form; false when no form can take it (no result handed in yet). */
export function requestCommentAt(taskId: string, anchor: CommentAnchor): boolean {
  const targets = listeners.get(taskId);
  if (!targets?.size) return false;
  for (const target of targets) target(anchor);
  return true;
}

/** Receive anchors for one task while ``enabled``; the latest handler is used without re-subscribing. */
export function useCommentAnchor(taskId: string, enabled: boolean, handler: (anchor: CommentAnchor) => void): void {
  const latest = useRef(handler);
  latest.current = handler;
  useEffect(() => {
    if (!enabled) return;
    const target = (anchor: CommentAnchor) => latest.current(anchor);
    const targets = listeners.get(taskId) ?? new Set();
    targets.add(target);
    listeners.set(taskId, targets);
    return () => {
      targets.delete(target);
      if (!targets.size && listeners.get(taskId) === targets) listeners.delete(taskId);
    };
  }, [taskId, enabled]);
}
