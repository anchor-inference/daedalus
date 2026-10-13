// @vitest-environment jsdom
// The Memory screen's topic helpers: typed topics read as the host keeps them, and the by-topic grouping.
import { describe, expect, it } from "vitest";
import type { MemoryRecord } from "./api";
import { byTopic, parseTopics } from "./screens/Memory";

const record = (id: string, topics: string[]): MemoryRecord => ({
  id, scope: "global", scope_key: "", category: "fact", topics, text: id, salience: 0, version: 1, created_at: null, updated_at: null, last_accessed_at: null,
});

describe("memory topics", () => {
  it("reads typed topics the way the host keeps them", () => {
    expect(parseTopics("#Billing API, billing-api; deploy\n , ")).toEqual(["billing-api", "deploy"]);
    expect(parseTopics("a, b, c, d, e, f")).toHaveLength(5);
  });

  it("puts a record under every topic it carries, biggest group first and the untagged last", () => {
    const groups = byTopic([record("a", ["bakery", "deploy"]), record("b", ["bakery"]), record("c", []), record("d", ["deploy"]), record("e", ["anna"])]);
    expect(groups.map(([topic, rs]) => [topic, rs.map((r) => r.id)])).toEqual([
      ["bakery", ["a", "b"]],
      ["deploy", ["a", "d"]],
      ["anna", ["e"]],
      ["", ["c"]],
    ]);
  });
});
