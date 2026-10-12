// The project picker's search: every word must be in the name or the path, in any case.

import { describe, expect, it } from "vitest";
import type { Project } from "./api";
import { filterProjects } from "./projectpick";

const project = (id: string, name: string, path: string) => ({ id, name, folders: [{ id: "f", path, position: 0, env: "container", reachable: true }], settings: {} }) as unknown as Project;

describe("finding a project to start a chat in", () => {
  const all = [project("a", "Bakery site", "/work/bakery"), project("b", "Support", "/work/support"), project("c", "Courier rates", "/docs/courier")];
  it("keeps every project for an empty query", () => {
    expect(filterProjects(all, "  ").map((p) => p.id)).toEqual(["a", "b", "c"]);
  });
  it("matches words in the name or the path, whatever the case", () => {
    expect(filterProjects(all, "BAK").map((p) => p.id)).toEqual(["a"]);
    expect(filterProjects(all, "work sup").map((p) => p.id)).toEqual(["b"]);
    expect(filterProjects(all, "docs").map((p) => p.id)).toEqual(["c"]);
    expect(filterProjects(all, "nothing like it")).toEqual([]);
  });
});
