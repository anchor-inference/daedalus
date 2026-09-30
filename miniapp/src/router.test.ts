// @vitest-environment jsdom
// A link to one message is an address with `#m<seq>` after it. Every rewrite on its way in — an old
// address made current, a plain session address moved to where the session lives — must carry that
// hash along, or the link opens the chat at its end as if it pointed at nothing.

import { afterEach, describe, expect, it } from "vitest";
import { canonical, markAnchor, navigate } from "./router";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("canonical", () => {
  it("keeps a message's hash through the rewrite of an old address", () => {
    expect(canonical("/app/project/p1#m42")).toBe("/app/orchestration/project/p1#m42");
    expect(canonical("/app/main#m42")).toBe("/app/orchestration#m42");
    expect(canonical("/app/main?panel=jobs#m42")).toBe("/app/orchestration?panel=jobs#m42");
    expect(canonical("/app/agents/s1#m42")).toBe("/app/agents/s1#m42");
  });
});

describe("navigate", () => {
  it("carries the hash along a redirect", () => {
    window.history.replaceState(null, "", "/app/agents/orch#m42");
    navigate("/app/orchestration/project/p1", { replace: true, keepHash: true });
    expect(window.location.pathname + window.location.hash).toBe("/app/orchestration/project/p1#m42");
  });

  it("drops it on an ordinary navigation, which goes somewhere else", () => {
    window.history.replaceState(null, "", "/app/agents/orch#m42");
    navigate("/app/agents/other");
    expect(window.location.hash).toBe("");
  });

  it("goes to a hash it is given, and leaves one alone when asked for the same place without one", () => {
    window.history.replaceState(null, "", "/app/agents/s1");
    navigate("/app/agents/s1#m7");
    expect(window.location.hash).toBe("#m7");
    navigate("/app/agents/s1");
    expect(window.location.hash).toBe("#m7");
  });

  it("writes the message the reader was taken to without leaving the page", () => {
    window.history.replaceState(null, "", "/app/orchestration?panel=jobs");
    markAnchor(9);
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/app/orchestration?panel=jobs#m9");
  });
});
