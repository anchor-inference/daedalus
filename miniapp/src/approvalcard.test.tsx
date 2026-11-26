// @vitest-environment jsdom
// The approval card in the composer's place: its three answers, the middle one's scope as the host
// says it (the project where a rule may be kept there, the chat where it may not), the fallback when the
// host refuses the project after all, and its keys.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { ApprovalCard } from "./approvalcard";

const calls = vi.hoisted(() => ({ get: {} as Record<string, unknown>, post: [] as [string, unknown][], refuse: false }));
vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return {
    ...real,
    api: {
      ...real.api,
      get: vi.fn((path: string) => Promise.resolve(calls.get[path] ?? {})),
      post: vi.fn((path: string, body: unknown) => {
        calls.post.push([path, body]);
        return calls.refuse ? Promise.reject(new real.ApiError(400, "the question 'git.force_push' is always asked again in each chat")) : Promise.resolve({});
      }),
    },
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const approval = { key: "0123456789ab", callId: "c1", tool: "Exec", detail: "rsync -az dist/ deploy@shop:/var/www/", reason: "a host outside the allowlist: shop" };

describe("the approval card", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    calls.get = {};
    calls.post = [];
    calls.refuse = false;
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  async function draw(over: Record<string, unknown> = {}) {
    const props = { sessionId: "s1", approval, phone: false, similar: "rsync* (shop)", toast: vi.fn(), onApprove: vi.fn(), onApproveSimilar: vi.fn(), onDeny: vi.fn(), onAnswered: vi.fn(), ...over };
    await act(async () => root.render(<ApprovalCard {...props} />));
    await act(async () => {});
    return props;
  }
  const widen = () => host.querySelector<HTMLButtonElement>("[data-action='allow-similar']")!;

  it("says what is asked, the command, why, and the three answers in order", async () => {
    await draw();
    expect(host.querySelector(".approval-eyebrow")!.textContent).toBe("Permission needed");
    expect(host.querySelector(".approval-command")!.textContent).toBe(approval.detail);
    expect(host.querySelector(".approval-why")!.textContent).toBe("A host outside the allowlist: shop");
    expect([...host.querySelectorAll(".approval-actions .btn")].map((b) => b.getAttribute("data-action"))).toEqual(["deny", "allow-similar", "allow"]);
  });

  it("allows similar calls in the chat where the host keeps no project rule for them", async () => {
    calls.get["/api/sessions/s1/policy/similar/0123456789ab"] = { project: { available: false, reason: "this chat belongs to no project" } };
    const props = await draw();
    expect(widen().textContent).toBe("Allow similar in this chat");
    expect(widen().title).toContain("rsync* (shop)");
    await act(async () => widen().click());
    expect(props.onApproveSimilar).toHaveBeenCalledWith(approval);
    expect(calls.post).toEqual([]);
  });

  it("allows them in the project where it may, and falls back to the chat, saying so, when the host refuses", async () => {
    calls.get["/api/sessions/s1/policy/similar/0123456789ab"] = { project: { available: true, project: { id: "p1", name: "Bakery site" } } };
    const props = await draw();
    expect(widen().textContent).toBe("Allow similar in this project");
    expect(widen().title).toContain("Bakery site");
    await act(async () => widen().click());
    expect(calls.post).toEqual([["/api/sessions/s1/policy/grant-project", { key: approval.key }]]);
    expect(props.onApproveSimilar).not.toHaveBeenCalled();
    expect(props.onAnswered).toHaveBeenCalledWith("settled");
    calls.refuse = true;
    await act(async () => widen().click());
    expect(props.onApproveSimilar).toHaveBeenCalledWith(approval);
    expect(props.toast).toHaveBeenCalledWith(expect.stringContaining("Allowed in this chat only"));
  });

  it("offers no widening for the agent's ask for the operator's Chrome", async () => {
    await draw({ approval: { ...approval, chrome: { name: "Work", reason: "sign in" } } });
    expect(widen()).toBeNull();
    expect(host.querySelector("[data-action='allow']")!.textContent).toContain("Use my Chrome");
  });

  it("allows once on Enter and refuses on Escape while nothing else has the keyboard", async () => {
    const props = await draw();
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); });
    expect(props.onApprove).toHaveBeenCalledWith(approval);
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
    expect(props.onDeny).toHaveBeenCalledWith(approval);
    expect(props.onAnswered).toHaveBeenCalledWith("deny");
    // A field being typed in keeps its own Enter.
    const field = document.createElement("textarea");
    document.body.appendChild(field);
    await act(async () => { field.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); });
    expect(props.onApprove).toHaveBeenCalledTimes(1);
    field.remove();
  });
});
