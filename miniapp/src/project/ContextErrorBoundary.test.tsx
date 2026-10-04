// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ContextErrorBoundary } from "./ContextErrorBoundary";

it("keeps the adjacent project context and hides the thrown detail", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  let fail = true;
  const log = vi.spyOn(console, "error").mockImplementation(() => {});
  function Detail() {
    if (fail) throw new Error("sk-private-token");
    return <div>{"detail recovered"}</div>;
  }
  try {
    await act(async () => root.render(<><div>{"project context"}</div><ContextErrorBoundary><Detail /></ContextErrorBoundary></>));
    expect(host.textContent).toContain("project context");
    expect(host.textContent).toContain("Retry");
    expect(host.textContent).not.toContain("sk-private-token");
    fail = false;
    await act(async () => (host.querySelector("button") as HTMLButtonElement).click());
    expect(host.textContent).toContain("detail recovered");
  } finally {
    await act(async () => root.unmount());
    host.remove();
    log.mockRestore();
  }
});
