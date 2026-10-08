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

  it("retires the bubble of a message that opened the next turn with a secret note appended", () => {
    // The host appends a line per attached secret, so the stored words never equal the typed ones; the
    // row that opened the next turn is the shown copy and has to name the receipt for the bubble to go.
    const typed = "here is the new password";
    const queued = { role: "user", internal: true, client_message_id: "send-1", text: typed } as MessageView;
    const opened = { role: "user", internal: false, delivery: "drained", client_message_ids: ["send-1"], text: `${typed}\n\n[The operator attached a secret for this project: «secret:db» (shell: $DAEDALUS_SECRET_DB). The value is not shown to you; use it by its placeholder.]` } as MessageView;
    expect(sentMessageReachedTranscript([queued], "send-1")).toBe(false);
    expect(sentMessageReachedTranscript([queued, opened], "send-1")).toBe(true);
    // Matching by the words could never retire it: they differ.
    expect(opened.text).not.toBe(typed);
  });
});
