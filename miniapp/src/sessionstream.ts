// One session's event stream, read for as long as a screen shows it. The conversation screen and the
// Subagents tab both follow a session this way: the same frames, the same resume point after a drop,
// the same resync when the host says the gap is too wide to replay.

import { api } from "./api";

export type SessionStreamHandlers = {
  /** One event off the wire, with the envelope's run id and sequence folded into its payload. */
  handle: (event: string, payload: Record<string, any>) => void;
  /** The stream ended or broke and is about to reconnect: whatever it was in the middle of is over,
   *  and the reader should re-read what it shows. */
  onDrop: () => void;
};

/** Follow a session's stream until the returned function is called; reconnects with backoff. */
export function followSession(sessionId: string, { handle, onDrop }: SessionStreamHandlers): () => void {
  const streamUrl = api.streamUrl(sessionId);
  const headers = api.authHeaders();
  let stop = false;
  let after: number | null = null;
  const controller = new AbortController();
  (async () => {
    let backoff = 1000;
    while (!stop) {
      try {
        const url = after === null ? streamUrl : `${streamUrl}?after=${after}`;
        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        backoff = 1000;
        while (!stop) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          let resync = false;
          for (const frame of frames) {
            const event = /^event: (.*)$/m.exec(frame)?.[1];
            const data = /^data: (.*)$/m.exec(frame)?.[1];
            if (!event || !data) continue;
            if (event === "resync_required") {
              try {
                const boundary = JSON.parse(data) as { watermark?: number };
                if (typeof boundary.watermark === "number") after = boundary.watermark;
              } catch {
                after = null;
              }
              resync = true;
              break;
            }
            try {
              const decoded = JSON.parse(data) as Record<string, any>;
              if (event === "hello" && typeof decoded.event_seq === "number") after = decoded.event_seq;
              const envelope = decoded.kind && decoded.payload && typeof decoded.payload === "object" ? decoded : null;
              if (envelope && typeof envelope.event_seq === "number") after = Math.max(after ?? 0, envelope.event_seq);
              handle(envelope ? String(envelope.kind) : event, envelope ? { ...envelope.payload, run_id: envelope.run_id, event_seq: envelope.event_seq, history_revision: envelope.history_revision } : decoded);
            } catch {
              /* one malformed frame must not end the stream */
            }
          }
          if (resync) {
            await reader.cancel();
            break;
          }
        }
      } catch {
        /* aborted or dropped: reconnect below */
      }
      if (stop) return;
      // The stream ended (server restart, proxy timeout): the reader re-reads and the stream reconnects.
      onDrop();
      await new Promise((r) => setTimeout(r, backoff));
      backoff = Math.min(backoff * 2, 15000);
    }
  })();
  return () => {
    stop = true;
    controller.abort();
  };
}
