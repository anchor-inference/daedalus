// Ticks that wait for the host. A checkbox in an event's view changes on the tap, and the patch goes
// out behind it; one that cannot reach the host (no network, the host restarting) is kept, on this
// device, and sent when the host answers again, so a list ticked off in a shop with no signal is not
// lost. A refusal (the item was removed meanwhile) is not kept: it is handed back for the screen to
// undo on screen and say why.
//
// Patches for one event are sent in the order they were made, one at a time: two ticks of the same
// item sent at once could arrive the other way round, and the last one to land is the one that wins.

import { ApiError } from "../api";
import { applyOps, type Block, type BlockOp } from "./blocks";

export type Pending = { seq: number; eventId: string; ops: BlockOp[]; at: number };
export type Sender = (eventId: string, ops: BlockOp[]) => Promise<unknown>;

const STORE = "daedalus.calendar.queue";
/** A tick kept longer than this is dropped rather than sent: a week-old tick sent today would undo
 *  what was done to that list on another device since. */
const MAX_AGE_MS = 7 * 86400000;

/** Whether a failure means the host was not reached, as opposed to the host saying no. A gateway that
 *  answers for a restarting host is "not reached" too. */
export function unreachable(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504;
}

type Storage = Pick<globalThis.Storage, "getItem" | "setItem" | "removeItem">;

function defaultStorage(): Storage | null {
  try { return typeof localStorage === "undefined" ? null : localStorage; } catch { return null; }
}

export class PatchQueue {
  private items: Pending[] = [];
  private seq = 0;
  private listeners = new Set<() => void>();

  constructor(private send: Sender, private storage: Storage | null = defaultStorage(), private now: () => number = Date.now) {
    try {
      const raw = this.storage?.getItem(STORE);
      const read = raw ? (JSON.parse(raw) as Pending[]) : [];
      this.items = Array.isArray(read) ? read.filter((p) => p && typeof p.eventId === "string" && Array.isArray(p.ops) && this.now() - p.at < MAX_AGE_MS) : [];
      this.seq = this.items.reduce((n, p) => Math.max(n, p.seq), 0);
    } catch { this.items = []; }
  }

  get size(): number {
    return this.items.length;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  /** The ops still waiting for one event, oldest first. */
  pendingFor(eventId: string): BlockOp[] {
    return this.items.filter((p) => p.eventId === eventId).flatMap((p) => p.ops);
  }

  /** The event's blocks as the host will have them once the waiting ops land: what the screen draws,
   *  so a read that comes back before the queue is sent does not untick what was ticked. */
  overlay(eventId: string, blocks: Block[]): Block[] {
    const ops = this.pendingFor(eventId);
    return ops.length ? applyOps(blocks, ops) : blocks;
  }

  /** Queues the ops and tries to send them. Resolves "sent" once the host took them, "queued" when the
   *  host could not be reached and they wait on this device; rejects with the host's refusal, the ops
   *  then dropped. */
  submit(eventId: string, ops: BlockOp[]): Promise<"sent" | "queued"> {
    const item: Pending = { seq: ++this.seq, eventId, ops, at: this.now() };
    this.items.push(item);
    this.save();
    return this.flush().then(() => {
      const error = this.refused.get(item.seq);
      if (error !== undefined) {
        this.refused.delete(item.seq);
        throw error;
      }
      return this.items.some((p) => p.seq === item.seq) ? "queued" : "sent";
    });
  }

  private refused = new Map<number, unknown>();
  private chain: Promise<void> = Promise.resolve();

  /** Sends what waits, in order, stopping at the first one the host cannot be reached for. Every call
   *  joins one chain, so two flushes never send the same patch twice or two patches out of order. */
  flush(): Promise<void> {
    this.chain = this.chain.then(() => this.drain());
    return this.chain;
  }

  private async drain(): Promise<void> {
    while (this.items.length) {
      const next = this.items[0];
      try {
        await this.send(next.eventId, next.ops);
        this.items.shift();
        this.save();
      } catch (error) {
        if (unreachable(error)) return;
        this.items.shift();
        this.refused.set(next.seq, error);
        this.save();
      }
    }
  }

  private save(): void {
    try {
      if (this.items.length) this.storage?.setItem(STORE, JSON.stringify(this.items));
      else this.storage?.removeItem(STORE);
    } catch { /* private mode: the queue lasts the visit */ }
    for (const listener of this.listeners) listener();
  }
}
