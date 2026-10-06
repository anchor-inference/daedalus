// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { stopReason } from "./TaskDiagnostics";

describe("why the host stopped a worker", () => {
  setLang("en");
  const seen = (oom: number, pids: number) => ({ observation_kind: "exit", memory_peak_bytes: 1, oom_kills: oom, pids_max_events: pids });

  it("names the memory limit when the kernel killed it for memory", () => {
    expect(stopReason({ kind: "cgroup_v2", limits: { memory_bytes: 2 * 1024 ** 3 }, observation: seen(1, 0) })).toContain("ran out of memory");
  });

  it("names the process limit when that is what it hit", () => {
    expect(stopReason({ kind: "cgroup_v2", limits: { process_count: 64 }, observation: seen(0, 3) })).toContain("64");
  });

  it("says nothing without a limit event or a record", () => {
    expect(stopReason({ kind: "cgroup_v2", observation: seen(0, 0) })).toBeNull();
    expect(stopReason({ kind: "none" })).toBeNull();
  });
});
