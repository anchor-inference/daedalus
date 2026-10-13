// @vitest-environment jsdom
// The two-factor card: what the desktop window shows instead of enrolment, and the QR code's drawing.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QrCode, TwoFactorCard } from "./twofactor";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function render(node: React.ReactNode): { box: HTMLDivElement; done: () => void } {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(node));
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

function answer(body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as { daedalus?: unknown }).daedalus;
});

describe("QrCode", () => {
  it("draws one square per dark module inside a quiet zone, dark on white", () => {
    const { box, done } = render(<QrCode rows={["101", "010", "101"]} label="code" />);
    const svg = box.querySelector("svg")!;
    expect(svg.getAttribute("viewBox")).toBe("0 0 11 11");
    expect(svg.querySelector("rect")!.getAttribute("fill")).toBe("#fff");
    expect(svg.querySelector("path")!.getAttribute("d")!.match(/M/g)).toHaveLength(5);
    done();
  });
});

describe("TwoFactorCard", () => {
  it("offers enrolment in a browser", async () => {
    vi.stubGlobal("fetch", answer({ enabled: false, enrolling: false, recovery_left: 0, enabled_at: null }));
    const { box, done } = render(<TwoFactorCard toast={() => undefined} />);
    await act(async () => undefined);
    expect(box.querySelector(".totp-card")!.getAttribute("data-totp")).toBe("off");
    expect([...box.querySelectorAll("button")].map((b) => b.textContent)).toContain("Set up");
    done();
  });

  it("offers no enrolment in the desktop window, and says why", async () => {
    (window as { daedalus?: unknown }).daedalus = { window: true };
    vi.stubGlobal("fetch", answer({ enabled: true, enrolling: false, recovery_left: 10, enabled_at: "2026-10-01T10:00:00Z" }));
    const { box, done } = render(<TwoFactorCard toast={() => undefined} />);
    await act(async () => undefined);
    expect(box.querySelectorAll("button")).toHaveLength(0);
    expect(box.textContent).toContain("launcher");
    done();
  });
});
