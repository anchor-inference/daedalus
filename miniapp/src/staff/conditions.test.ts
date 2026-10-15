import { afterEach, describe, expect, it } from "vitest";
import type { MemberCondition } from "../api";
import { setLang } from "../i18n";
import { blockingCondition, conditionText, conditionTone } from "./model";

const cond = (over: Partial<MemberCondition>): MemberCondition => ({
  name: "first_output", status: "true", reason: "done", message: "", since: null, deadline: null, params: {}, ...over,
});

describe("member conditions", () => {
  afterEach(() => setLang("en"));

  it("reads a failed step before a waiting one, and nothing when all are through", () => {
    const waiting = cond({ name: "ready", status: "false", reason: "waiting", deadline: "2026-10-14T18:15:05Z" });
    const failed = cond({ status: "false", reason: "timed_out", since: "2026-10-14T18:15:05Z" });
    expect(blockingCondition([cond({ name: "prepared" }), waiting, failed])).toBe(failed);
    expect(blockingCondition([cond({ name: "prepared" }), waiting])).toBe(waiting);
    expect(blockingCondition([cond({}), cond({ name: "reported", status: "unknown", reason: "not_yet" })])).toBeNull();
    expect([conditionTone(waiting), conditionTone(failed), conditionTone(cond({})), conditionTone(cond({ status: "unknown" }))]).toEqual(["wait", "bad", "ok", "unknown"]);
  });

  it("says how long a wait has run and when the host gives up", () => {
    setLang("ru");
    const since = "2026-10-14T18:10:05Z";
    const text = conditionText(cond({ status: "false", reason: "waiting", since, deadline: "2026-10-14T18:15:05Z", params: { elapsed_s: 10 } }),
      Date.parse(since) + 5 * 60_000);
    expect(text.startsWith("нет 5 мин, срок ")).toBe(true);
  });

  it("names the deadline that stopped a member, and the signals that were not output", () => {
    const text = conditionText(cond({ status: "false", reason: "timed_out", since: "2026-10-14T18:15:05Z",
      params: { allowed_s: 300, last_signal_at: "2026-10-14T18:14:43Z" } }));
    expect(text).toMatch(/^none within 5m; stopped at .+ · signals until .+, no output$/);
    expect(conditionText(cond({ name: "reported", reason: "reported", params: { outcome: "partial" } }))).toBe("handed in: partly");
    expect(conditionText(cond({ reason: "done", params: { took_s: 4 } }))).toBe("done in 4s");
  });
});
