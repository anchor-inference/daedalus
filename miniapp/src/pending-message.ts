import type { MessageView } from "./api";

/**
 * Matched by receipt id, never by words: an upload, a reply quote and an attached secret all change the
 * stored text. A queued row is hidden, so the bubble waits for the shown copy — the one the core placed
 * mid-run or the one that opened the next turn — and that copy must carry the id, or the bubble stays.
 */
export function sentMessageReachedTranscript(messages: MessageView[] | undefined, clientMessageId: string): boolean {
  return messages?.some((message) => message.role === "user" && !message.internal && (message.client_message_id === clientMessageId || message.client_message_ids?.includes(clientMessageId))) ?? false;
}
