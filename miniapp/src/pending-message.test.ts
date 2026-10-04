import { describe, expect, it } from "vitest";
import { sentMessageReachedTranscript } from "./pending-message";
import type { MessageView } from "./api";

describe("temporary chat messages", () => {
  it("uses the client id when an upload changes the stored text", () => {
    const message = { role: "user", client_message_id: "send-1", text: "Attached files:\n- sample.txt", created_at: "2020-01-01T00:00:00Z" } as MessageView;
    expect(sentMessageReachedTranscript([message], "send-1")).toBe(true);
    expect(sentMessageReachedTranscript([message], "send-2")).toBe(false);
  });

  it("does not replace a pending message with another user's identical words", () => {
    const message = { role: "user", client_message_id: "earlier", text: "same words" } as MessageView;
    expect(sentMessageReachedTranscript([message], "later")).toBe(false);
  });

  it("keeps a queued bubble until the visible received copy arrives", () => {
    const queued = { role: "user", internal: true, client_message_id: "send-1" } as MessageView;
    const received = { role: "user", internal: false, client_message_ids: ["send-1", "send-2"] } as MessageView;
    expect(sentMessageReachedTranscript([queued], "send-1")).toBe(false);
    expect(sentMessageReachedTranscript([queued, received], "send-1")).toBe(true);
    expect(sentMessageReachedTranscript([queued, received], "send-2")).toBe(true);
  });
});
