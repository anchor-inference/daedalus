// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import type { Block, BlockOp, ChecklistBlock } from "./blocks";
import { PatchQueue, unreachable } from "./patchqueue";

function memory() {
  const data = new Map<string, string>();
  return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => { data.set(k, v); }, removeItem: (k: string) => { data.delete(k); }, data };
}

const list: Block[] = [{ id: "c1", kind: "checklist", items: [{ id: "a", text: "Milk", checked: false }, { id: "b", text: "Eggs", checked: false }] } as ChecklistBlock];
const tick = (item: string, checked = true): BlockOp => ({ op: "set_checked", block_id: "c1", item_id: item, checked });
const checked = (blocks: Block[]) => (blocks[0] as ChecklistBlock).items.filter((i) => i.checked).map((i) => i.id);

describe("ticks that wait for the host", () => {
  it("sends a tick and forgets it once the host took it", async () => {
    const sent: BlockOp[][] = [];
    const queue = new PatchQueue(async (_id, ops) => { sent.push(ops); }, memory());
    await expect(queue.submit("e1", [tick("a")])).resolves.toBe("sent");
    expect(sent).toEqual([[tick("a")]]);
    expect(queue.size).toBe(0);
  });

  it("shows a tick at once, before the host answers", async () => {
    let release!: () => void;
    const queue = new PatchQueue(() => new Promise<void>((resolve) => { release = resolve; }), memory());
    const done = queue.submit("e1", [tick("a")]);
    expect(checked(queue.overlay("e1", list))).toEqual(["a"]);
    expect(checked(queue.overlay("other", list))).toEqual([]);
    await Promise.resolve();
    release();
    await done;
    expect(checked(queue.overlay("e1", list))).toEqual([]);
  });

  it("keeps ticks on the device while the host cannot be reached, in order, and sends them later", async () => {
    const storage = memory();
    let online = false;
    const sent: string[] = [];
    const queue = new PatchQueue(async (_id, ops) => {
      if (!online) throw new TypeError("Failed to fetch");
      sent.push((ops[0] as { item_id: string }).item_id);
    }, storage);
    await expect(queue.submit("e1", [tick("a")])).resolves.toBe("queued");
    await expect(queue.submit("e1", [tick("b")])).resolves.toBe("queued");
    expect(queue.size).toBe(2);
    expect(checked(queue.overlay("e1", list))).toEqual(["a", "b"]);
    // A reload in a shop with no signal: the next page reads the same queue back.
    const reread = new PatchQueue(async () => undefined, storage);
    expect(reread.pendingFor("e1")).toEqual([tick("a"), tick("b")]);
    online = true;
    await queue.flush();
    expect(sent).toEqual(["a", "b"]);
    expect(queue.size).toBe(0);
    expect(storage.data.size).toBe(0);
  });

  it("hands back a refusal and drops the tick, so the screen shows the truth again", async () => {
    const queue = new PatchQueue(async () => { throw new ApiError(404, "No such item"); }, memory());
    await expect(queue.submit("e1", [tick("a")])).rejects.toThrow("No such item");
    expect(queue.size).toBe(0);
    expect(checked(queue.overlay("e1", list))).toEqual([]);
  });

  it("undoes a tick by sending its inverse after it", async () => {
    const sent: BlockOp[][] = [];
    const queue = new PatchQueue(async (_id, ops) => { sent.push(ops); }, memory());
    await queue.submit("e1", [tick("a", true)]);
    await queue.submit("e1", [tick("a", false)]);
    expect(sent.map((ops) => (ops[0] as { checked: boolean }).checked)).toEqual([true, false]);
  });

  it("drops what was kept for more than a week", () => {
    const storage = memory();
    storage.setItem("daedalus.calendar.queue", JSON.stringify([{ seq: 1, eventId: "e1", ops: [tick("a")], at: 0 }]));
    const queue = new PatchQueue(async () => undefined, storage, () => 8 * 86400000);
    expect(queue.size).toBe(0);
  });

  it("tells a host that was not reached from one that said no", () => {
    expect(unreachable(new TypeError("Failed to fetch"))).toBe(true);
    expect(unreachable(new ApiError(503, "down"))).toBe(true);
    expect(unreachable(new ApiError(409, "changed"))).toBe(false);
  });
});
