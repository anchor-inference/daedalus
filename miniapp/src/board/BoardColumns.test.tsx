// @vitest-environment jsdom
// The board's columns as a chat's panel will mount them: stacked, compact, the finished folded, and a
// refusal said on the card that was moved.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { BoardColumns, stackedColumns, type ChatInfo } from "./BoardColumns";
import type { BoardTask } from "./tasks";

let host: HTMLDivElement;
let root: Root;

const task = (id: string, extra: Partial<BoardTask> = {}): BoardTask => ({
  id, title: `Card ${id}`, status: "todo", priority: 3, acceptance: "", checklist: [], depends_on: [], session_id: "s1", origin_session_id: "s1",
  notes: "", created_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-24T10:00:00Z", ...extra,
});
const chats: ChatInfo = { title: () => "Menu", running: (id) => id === "s1", waiting: () => false };

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("a chat's board, stacked", () => {
  it("reads doing, to do, review, blocked, then the finished folded", () => {
    const tasks = [task("a", { status: "doing", checklist: [{ text: "x", done: true }, { text: "y", done: false }] }), task("b"), task("c", { status: "done" }), task("d", { status: "blocked" })];
    const columns = stackedColumns(tasks);
    expect(columns.map((c) => [c.key, c.tasks.map((x) => x.id)])).toEqual([["doing", ["a"]], ["todo", ["b"]], ["review", []], ["blocked", ["d"]], ["finished", ["c"]]]);
    const open = vi.fn();
    act(() => root.render(<BoardColumns columns={columns} scope="chat" layout="stacked" onOpen={open} chats={chats} />));
    expect(host.querySelector(".board-cols.stacked")).not.toBeNull();
    // The finished cards wait behind their column's fold.
    expect(host.querySelector("[data-task='c']")).toBeNull();
    const doing = host.querySelector<HTMLButtonElement>("[data-task='a']")!;
    expect(doing.classList.contains("compact")).toBe(true);
    expect(doing.querySelector(".task-card-line")!.textContent).toBe("working · Agent · 1/2");
    act(() => host.querySelector<HTMLButtonElement>(".kanban-fold")!.click());
    expect(host.querySelector("[data-task='c']")).not.toBeNull();
    act(() => doing.click());
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
  });

  it("says a refused move on the card that was moved", () => {
    const columns = stackedColumns([task("a")]);
    act(() => root.render(<BoardColumns columns={columns} scope="chat" layout="stacked" onOpen={() => undefined} chats={chats} refusal={{ taskId: "a", where: "card", text: "Not now" }} />));
    expect(host.querySelector("[data-task='a'] [role=alert]")!.textContent).toBe("Not now");
  });
});
