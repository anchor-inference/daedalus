// The chats a board names: a title for a card's chat, whether it is at work or waits on the operator.
// The Board page and a chat's own Board tab read the same listing, so a card says the same thing in both.

import { useMemo } from "react";
import type { SessionList, SessionSummary } from "../api";
import { useQuery } from "../store";
import type { ChatInfo } from "./BoardColumns";

/** The chats the board can name and narrow to: the operator's own, not a helper of one. */
export function useChats(): { list: SessionSummary[]; info: ChatInfo } {
  const { data } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  return useMemo(() => {
    const sessions = data?.sessions ?? [];
    const byId = new Map(sessions.map((s) => [s.id, s]));
    return {
      list: sessions.filter((s) => !s.metadata?.subagent_of && !s.archived).sort((a, b) => Date.parse(b.last_message_at) - Date.parse(a.last_message_at)),
      info: {
        title: (id) => byId.get(id)?.title,
        running: (id) => byId.get(id)?.status === "running",
        waiting: (id) => { const s = byId.get(id); return Boolean(s && (s.needs_attention || s.status === "waiting")); },
      },
    };
  }, [data]);
}
