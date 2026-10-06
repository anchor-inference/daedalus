import type { MessageView } from "./api";

/** A file upload changes the stored text; a queued row is hidden until the core places it. */
export function sentMessageReachedTranscript(messages: MessageView[] | undefined, clientMessageId: string): boolean {
  return messages?.some((message) => message.role === "user" && !message.internal && (message.client_message_id === clientMessageId || message.client_message_ids?.includes(clientMessageId))) ?? false;
}
