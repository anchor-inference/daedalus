import { describe, expect, it } from "vitest";
import type { ProjectDir } from "./api";
import { agentFolders, folderBase, folderName, needsMount, offersFolderChoice, pathProblem, primaryFolder, projectPath, projectReachable, reachIsProblem, reachKey } from "./folders";

function dir(id: string, over: Partial<ProjectDir> = {}): ProjectDir {
  return { id, path: `/work/${id}`, label: "", env: "container", is_git: false, readonly: false, position: 0, managed: false, reachable: true, writable: true, reach: "agents", ...over };
}

describe("a project's folders", () => {
  it("names the first folder as the primary", () => {
    const project = { folders: [dir("site"), dir("docs", { position: 1 })] };
    expect(primaryFolder(project)?.id).toBe("site");
    expect(projectPath(project)).toBe("/work/site");
  });

  it("is reachable when its primary folder is, whatever the others are", () => {
    expect(projectReachable({ folders: [dir("site", { reachable: false }), dir("docs", { position: 1 })] })).toBe(false);
    expect(projectReachable({ folders: [dir("site"), dir("docs", { position: 1, reachable: false })] })).toBe(true);
  });

  it("is reachable through the host daemon when its primary folder is on the host", () => {
    expect(projectReachable({ folders: [dir("tools", { env: "host", reach: "terminals", reachable: false })] })).toBe(true);
    expect(projectReachable({ folders: [dir("tools", { env: "host", reach: "none", reachable: false })] })).toBe(false);
  });

  it("answers for a project with no folder rather than throwing", () => {
    expect(primaryFolder({ folders: [] })).toBeUndefined();
    expect(projectPath({ folders: [] })).toBe("");
    expect(projectReachable({ folders: [] })).toBe(false);
  });

  it("calls a folder by its label, else by the last part of its path", () => {
    expect(folderName(dir("site", { label: " Shop " }))).toBe("Shop");
    expect(folderName(dir("site"))).toBe("site");
    expect(folderName(dir("x", { path: "C:\\work\\docs\\" }))).toBe("docs");
  });

  it("offers a new agent a folder only among those the bot can work in", () => {
    const one = { folders: [dir("site"), dir("tools", { env: "host", reach: "terminals" })] };
    // A host folder is one a native agent can work in too, through the host daemon.
    expect(agentFolders(one).map((f) => f.id)).toEqual(["site", "tools"]);
    expect(offersFolderChoice(one)).toBe(true);
    expect(offersFolderChoice({ folders: [dir("site"), dir("tools", { env: "host", reach: "none", reachable: false })] })).toBe(false);
    expect(offersFolderChoice({ folders: [dir("site"), dir("docs")] })).toBe(true);
    // A session's own copy of its project carries no reach and offers nothing rather than guessing.
    expect(offersFolderChoice({ folders: [dir("site", { reach: undefined }), dir("docs", { reach: undefined })] })).toBe(false);
  });
});

describe("the sentence under a folder", () => {
  const docker = { docker: true };
  it("says who can reach a host folder instead of asking for a mount", () => {
    expect(reachKey(dir("tools", { env: "host", reach: "terminals", reachable: false }), docker)).toBe("folder.reach.terminals");
    expect(reachKey(dir("tools", { env: "host", reach: "none", reachable: false }), docker)).toBe("folder.reach.none");
  });

  it("asks for a mount in Docker and for the folder back natively", () => {
    expect(reachKey(dir("site", { reachable: false }), docker)).toBe("folder.reach.unmounted");
    expect(reachKey(dir("site", { reachable: false }), { docker: false })).toBe("folder.reach.missing");
    expect(reachIsProblem("folder.reach.unmounted")).toBe(true);
  });

  it("tells a locked folder from one the bot may not write", () => {
    expect(reachKey(dir("site", { readonly: true, writable: false }), docker)).toBe("folder.reach.readonly");
    expect(reachKey(dir("site", { writable: false }), docker)).toBe("folder.reach.notwritable");
    expect(reachKey(dir("site"), docker)).toBe("folder.reach.ok");
    expect(reachIsProblem("folder.reach.ok")).toBe(false);
  });

  it("warns about the mount only for a container folder in Docker", () => {
    expect(needsMount("container", docker)).toBe(true);
    expect(needsMount("host", docker)).toBe(false);
    expect(needsMount("container", { docker: false })).toBe(false);
    expect(needsMount("container", undefined)).toBe(false);
  });

  it("takes a full path on either system and nothing else", () => {
    expect(pathProblem("")).toBe(false);
    expect(pathProblem("/work/site")).toBe(false);
    expect(pathProblem("D:\\work")).toBe(false);
    expect(pathProblem("work/site")).toBe(true);
  });
});

describe("a session's file pane across folders", () => {
  it("reads the session's own folder at the session's address and another under /folders", () => {
    expect(folderBase("/api/sessions/s1", "", "f-site")).toBe("/api/sessions/s1");
    expect(folderBase("/api/sessions/s1", "f-site", "f-site")).toBe("/api/sessions/s1");
    expect(folderBase("/api/sessions/s1", "f-docs", "f-site")).toBe("/api/sessions/s1/folders/f-docs");
    expect(folderBase("/api/sessions/s1", "f/x", "f-site")).toBe("/api/sessions/s1/folders/f%2Fx");
  });

  it("names a folder by its label, else by the last part of its path", () => {
    expect(folderName({ label: "Docs", path: "/work/docs" })).toBe("Docs");
    expect(folderName({ label: "  ", path: "/work/site/" })).toBe("site");
    expect(folderName({ label: "", path: "/" })).toBe("/");
  });
});
