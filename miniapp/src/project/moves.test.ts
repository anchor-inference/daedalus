// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { moveLine, movePath, movePercent } from "./moves";

describe("a running move as the app shows it", () => {
  it("says how far it is in whole percent, never past the whole", () => {
    expect(movePercent({ state: "copying", done_bytes: 0, total_bytes: 0 })).toBe(0);
    expect(movePercent({ state: "copying", done_bytes: 249, total_bytes: 591 })).toBe(42);
    expect(movePercent({ state: "copying", done_bytes: 700, total_bytes: 591 })).toBe(100);
    expect(movePercent({ state: "done", done_bytes: 0, total_bytes: 591 })).toBe(100);
  });
  it("names the direction while it copies and that it is finishing after", () => {
    setLang("en");
    expect(moveLine({ direction: "to_host", state: "copying", done_bytes: 42, total_bytes: 100 })).toBe("Moving to the host… 42 %");
    expect(moveLine({ direction: "to_container", state: "preparing", done_bytes: 0, total_bytes: 0 })).toBe("Moving into the container… 0 %");
    expect(moveLine({ direction: "to_host", state: "switching", done_bytes: 100, total_bytes: 100 })).toBe("Finishing the move…");
    setLang("ru");
    expect(moveLine({ direction: "to_host", state: "copying", done_bytes: 42, total_bytes: 100 })).toBe("Переносится на хост… 42 %");
    setLang("en");
  });
  it("opens the chat whose scratch folder moves, or the project", () => {
    const chat = { id: "p1", settings: { snapshots: true, ephemeral: true }, sessions: [{ id: "s1", title: "chat" }] };
    const project = { id: "p2", settings: { snapshots: true }, sessions: [{ id: "s2", title: "x" }] };
    const projects = [chat, project] as never;
    expect(movePath({ project_id: "p1" }, projects)).toContain("s1");
    expect(movePath({ project_id: "p2" }, projects)).toContain("p2");
  });
});
