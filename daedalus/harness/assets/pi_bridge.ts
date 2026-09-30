/**
 * Daedalus's bridge into pi: what makes a pi session a staff member.
 *
 * pi has no hooks, no MCP and no server, but it loads extensions, and an extension sees every event
 * of the session and can hand the agent a message. So this one file, written into the launch's
 * directory and loaded with `-e`, is the whole channel between the host and a pi staff member:
 *
 * - it posts the session's events to the launch's hook listener (`$DAEDALUS_HOOK_URL/pi`), in the
 *   order they happened: `ready` once the session exists, `agent_start`, `tool_start` / `tool_end`,
 *   `input` when a prompt is taken in (the acknowledgement of a message, by the id it was sent with),
 *   `agent_end` with the run's last words, `agent_settled` once pi will not go on by itself, and
 *   `session_end`;
 * - it listens on `$DAEDALUS_DIAL_DIR/pi.sock`, where the terminal daemon lets the host dial, for one
 *   JSON object per line: `{"op": "send", "id", "text", "deliverAs"?}`, `{"op": "abort"}` and
 *   `{"op": "state"}`, each answered with one line;
 * - it gives the model the team's two tools, `Report` and `AskOrchestrator`, speaking the wire
 *   contract of the daemon's `team-mcp` (the other CLIs' route to the same tools): a post to
 *   `$DAEDALUS_HOOK_URL/team?wait_ms=<hold>` with a call id, the host's reply as the result;
 * - it gives the model the tools of each set named in `$DAEDALUS_TOOL_SETS` (the browser's), read
 *   from `$DAEDALUS_LAUNCH_DIR/tools/<set>.json` and speaking the wire of the daemon's `tools-mcp`:
 *   a post to `$DAEDALUS_HOOK_URL/tools` with the set, the tool, its arguments and a call id;
 * - it answers pi's project-trust question for the launch, which the operator settled by choosing
 *   the folder.
 *
 * Without `$DAEDALUS_HOOK_URL` it does nothing at all: it is only ever loaded by a launch, and a pi
 * started by hand with it must behave as plain pi.
 *
 * Every post is best effort and never holds pi up: a host that is away costs a staff member its
 * status, never its work. Only the team tools wait, because their answer is the result.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { readFileSync, unlinkSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { join } from "node:path";

const HOOK_URL = (process.env.DAEDALUS_HOOK_URL ?? "").replace(/\/+$/, "");
const TOKEN = process.env.DAEDALUS_HOOK_TOKEN ?? "";
const DIAL_DIR = process.env.DAEDALUS_DIAL_DIR ?? "";
const LAUNCH = process.env.DAEDALUS_LAUNCH_ID || "nolaunch";
const ASK_HOLD_MS = Number(process.env.DAEDALUS_ASK_HOLD_MS || 300_000);
const REPORT_HOLD_MS = Number(process.env.DAEDALUS_REPORT_HOLD_MS || 15_000);
const LAUNCH_DIR = process.env.DAEDALUS_LAUNCH_DIR ?? "";
const TOOL_SETS = (process.env.DAEDALUS_TOOL_SETS ?? "").split(",").map((name) => name.trim()).filter((name) => /^[a-z][a-z0-9_-]{0,31}$/.test(name));
const TOOLS_HOLD_MS = Number(process.env.DAEDALUS_TOOLS_HOLD_MS || 330_000);
/** A post to the listener gives up after this beyond its hold: the host being away must never
 * stall a turn. */
const EVENT_TIMEOUT_MS = 10_000;
/** A line on the bridge socket longer than this is dropped whole: no message is that long. */
const LINE_MAX = 4 << 20;
/** How much of a prompt the `input` event carries back: enough to match it, not the whole brief. */
const TEXT_EXCERPT = 4000;

const RECORDED = "recorded";
const EXPIRED =
  "Pending: nobody has answered yet. The answer will arrive as a message; carry on with what the brief allows " +
  "meanwhile, or end your turn and wait for it. Do not ask the same question again.";
const GONE = "this session is no longer connected to its team; nobody received the call";

let calls = 0;
/** Call ids are `<launch>:<process>:<n>`, as `team-mcp` makes them, so the host drops a repeat. */
const CALL_PREFIX = `${LAUNCH}:${process.pid.toString(16).padStart(8, "0")}`;

type Json = Record<string, unknown>;

async function postJson(path: string, body: Json, waitMs: number, signal?: AbortSignal): Promise<{ status: number; text: string }> {
  const url = `${HOOK_URL}/${path}${waitMs > 0 ? `?wait_ms=${waitMs}` : ""}`;
  const timeout = AbortSignal.timeout(waitMs + EVENT_TIMEOUT_MS);
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  return { status: response.status, text: await response.text() };
}

/** Events go out one after another, never side by side, so the listener sees them in pi's order. */
let chain: Promise<void> = Promise.resolve();

function emit(event: string, fields: Json = {}): void {
  const body = { event, at: new Date().toISOString(), ...fields };
  chain = chain.then(
    () => postJson("pi", body, 0).then(() => undefined, () => undefined),
  );
}

/** The host's reply as the tool's result: an object's `text` (`error: true` marks a refusal), its
 * `detail`, a JSON string, or plain text. Empty means the host said nothing in time. */
function replyText(raw: string): { text: string; error: boolean } {
  const trimmed = raw.trim();
  if (!trimmed) return { text: "", error: false };
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (typeof parsed === "string") return { text: parsed, error: false };
    if (parsed && typeof parsed === "object") {
      const obj = parsed as Json;
      const error = obj.error === true;
      for (const key of ["text", "detail"]) {
        if (typeof obj[key] === "string") return { text: obj[key] as string, error };
      }
      return { text: trimmed, error };
    }
  } catch {
    // Not JSON: plain text is the answer as it stands.
  }
  return { text: trimmed, error: false };
}

async function teamCall(tool: "report" | "ask", fields: Json, fallback: string, holdMs: number, signal?: AbortSignal): Promise<string> {
  calls += 1;
  const body = { tool, ...fields, call_id: `${CALL_PREFIX}:${calls}` };
  let result: { status: number; text: string };
  try {
    result = await postJson("team", body, holdMs, signal);
  } catch (error) {
    if (signal?.aborted) throw new Error("the call was cancelled");
    // No address in the message: the model has no use for it, and it names the host's network.
    throw new Error("the team could not be reached; carry on with what the brief allows and try again later");
  }
  if (result.status >= 200 && result.status < 300) {
    const reply = replyText(result.text);
    if (reply.error) throw new Error(reply.text);
    return reply.text || fallback;
  }
  if (result.status === 401 || result.status === 410) throw new Error(GONE);
  if (result.status === 429) throw new Error("too many calls at once; wait a moment and call again");
  const reply = replyText(result.text);
  throw new Error(`the team refused the call (${result.status}): ${reply.text}`);
}

type SetTool = { name: string; description: string; inputSchema: Record<string, unknown> };
type SetFile = { unanswered?: string; hold_ms?: number; tools?: SetTool[] };

/** A tool set the host wrote into the launch; a set that cannot be read gives no tools, and the
 * host, which hears no hello from it, says so. */
function readSet(name: string): SetFile | undefined {
  if (!LAUNCH_DIR) return undefined;
  try {
    return JSON.parse(readFileSync(join(LAUNCH_DIR, "tools", `${name}.json`), "utf8")) as SetFile;
  } catch {
    return undefined;
  }
}

/** One call of a set's tool, on the wire of the daemon's `tools-mcp`. Silence is never a success:
 * the action may or may not have happened, and the model is told to look before it tries again. */
async function setCall(set: string, tool: string, args: Json, unanswered: string, holdMs: number, signal?: AbortSignal): Promise<string> {
  calls += 1;
  const body = { set, tool, arguments: args, call_id: `${CALL_PREFIX}:${calls}` };
  let result: { status: number; text: string };
  try {
    result = await postJson("tools", body, holdMs, signal);
  } catch (error) {
    if (signal?.aborted) throw new Error("the call was cancelled");
    throw new Error("Daedalus could not be reached; the action was not taken");
  }
  if (result.status >= 200 && result.status < 300) {
    const reply = replyText(result.text);
    if (!reply.text) throw new Error(unanswered);
    if (reply.error) throw new Error(reply.text);
    return reply.text;
  }
  if (result.status === 401 || result.status === 410) throw new Error(GONE);
  if (result.status === 429) throw new Error("too many calls at once; wait a moment and call again");
  throw new Error(`Daedalus refused the call (${result.status}): ${replyText(result.text).text}`);
}

function text(value: string) {
  return { content: [{ type: "text" as const, text: value }], details: {} };
}

/** The text of the last assistant message of a run: what an implicit report quotes when the turn
 * ended without one. */
function lastWords(messages: unknown[]): { text: string; stopReason: string; errorMessage: string } {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i] as Json;
    if (message?.role !== "assistant") continue;
    const content = Array.isArray(message.content) ? (message.content as Json[]) : [];
    const words = content
      .filter((block) => block?.type === "text" && typeof block.text === "string")
      .map((block) => block.text as string)
      .join("\n")
      .trim();
    return { text: words, stopReason: String(message.stopReason ?? ""), errorMessage: String(message.errorMessage ?? "") };
  }
  return { text: "", stopReason: "", errorMessage: "" };
}

export default function (pi: ExtensionAPI) {
  if (!HOOK_URL) return;

  let context: ExtensionContext | undefined;
  let server: Server | undefined;
  let socketPath = "";
  let last = { text: "", stopReason: "", errorMessage: "" };
  /** Messages sent through the socket and not yet taken in, as [id, text]: the `input` event names
   * no id, so a message is known by its text, oldest first. */
  const pending: Array<[string, string]> = [];

  // The operator chose this folder for the member; pi's own question would stop the launch at a
  // prompt nobody reads. Not remembered: it holds for this process only.
  pi.on("project_trust", async () => ({ trusted: "yes" }));

  pi.on("session_start", async (event, ctx) => {
    context = ctx;
    startSocket();
    emit("ready", {
      reason: event.reason,
      sessionId: ctx.sessionManager.getSessionId(),
      sessionFile: ctx.sessionManager.getSessionFile() ?? "",
      cwd: ctx.cwd,
      model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "",
    });
    // The team tools are this extension's, registered below: the host hears they are loaded the
    // same way it hears from the MCP bridge of the other CLIs.
    postJson("team", { tool: "hello", stage: "extension", client: { name: "pi" } }, 0).catch(() => undefined);
  });

  pi.on("input", async (event) => {
    let id = "";
    if (event.source === "extension") {
      const index = pending.findIndex(([, sent]) => sent === event.text);
      if (index >= 0) {
        id = pending[index][0];
        pending.splice(index, 1);
      }
    }
    emit("input", { id, source: event.source, text: event.text.slice(0, TEXT_EXCERPT), streamingBehavior: event.streamingBehavior ?? "" });
    return { action: "continue" };
  });

  pi.on("agent_start", async () => emit("agent_start"));

  // Execution, not `tool_call`: that one may block a tool, and this bridge never decides one.
  pi.on("tool_execution_start", async (event) => emit("tool_start", { toolName: event.toolName, toolCallId: event.toolCallId }));
  pi.on("tool_execution_end", async (event) => emit("tool_end", { toolName: event.toolName, toolCallId: event.toolCallId, isError: event.isError }));

  pi.on("agent_end", async (event) => {
    last = lastWords(event.messages as unknown[]);
    emit("agent_end", { lastMessage: last.text, stopReason: last.stopReason, errorMessage: last.errorMessage });
  });

  // The turn is over only here: after `agent_end` pi may still retry, compact, or take a queued
  // follow-up, and a status that said "done" in between would be wrong.
  pi.on("agent_settled", async () => emit("agent_settled", { lastMessage: last.text, stopReason: last.stopReason, errorMessage: last.errorMessage }));

  pi.on("session_shutdown", async (event) => {
    emit("session_end", { reason: event.reason });
    stopSocket();
    await chain;
  });

  pi.registerTool({
    name: "Report",
    label: "Report",
    description:
      "Tell your team how your task stands. kind: 'checkpoint' (progress worth knowing), 'needs_input' (you cannot go " +
      "on without a decision), 'stuck' (something outside your task blocks you) or 'done' (the deliverable meets the " +
      "task's done-when; it hands the task in: work on your own branch goes to review for the operator to merge, and a " +
      "worktree with uncommitted changes is refused — commit first; any other task goes to done, handed in for the " +
      "orchestrator to check against your note). note: a short factual summary. evidence (with done): [{item, how, " +
      "result}] for each check (C1 …) and requirement (R1 …) of the task — what you ran or looked at and what it " +
      "showed. acknowledged: the requirements (R…) sent to you that you have taken into your plan, or the input files " +
      "(R…) you have read. artifacts: paths or links of what you produced. remember: one line to keep in your notes for " +
      "every later session.",
    promptSnippet: "Report progress, a needed decision, a blocker or the finished task to your team",
    promptGuidelines: ["End every turn with a Report call: the orchestrator hears from you only through Report and AskOrchestrator."],
    parameters: Type.Object({
      kind: StringEnum(["checkpoint", "needs_input", "stuck", "done"] as const),
      note: Type.String({ description: "A short factual summary." }),
      artifacts: Type.Optional(Type.Array(Type.String())),
      remember: Type.Optional(Type.String({ description: "One line for your notes." })),
      evidence: Type.Optional(
        Type.Array(
          Type.Object({
            item: Type.String({ description: "The check or requirement: C1, R2, or its words." }),
            how: Type.String({ description: "What you ran, opened or looked at to check it." }),
            result: Type.String({ description: "What that showed." }),
          }),
        ),
      ),
      acknowledged: Type.Optional(Type.Array(Type.String(), { description: "Requirements (R…) you have taken into your plan." })),
      operator_steps: Type.Optional(
        Type.Object({
          goal: Type.String(),
          steps: Type.Array(Type.String()),
          roles: Type.Optional(Type.Array(Type.Object({ account: Type.String(), purpose: Type.String() }))),
          expected: Type.Optional(Type.String()),
          check: Type.Optional(Type.String()),
          limits: Type.Optional(Type.String()),
          verified: StringEnum(["on-running-version", "unverified"] as const),
          verified_how: Type.Optional(Type.String()),
        }, { description: "Steps the operator follows themselves, delivered to them word for word; never a password in them." }),
      ),
    }),
    async execute(_id, params, signal) {
      const fields: Json = { kind: params.kind, note: params.note, artifacts: params.artifacts ?? [] };
      if (params.remember?.trim()) fields.remember = params.remember;
      if (params.evidence?.length) fields.evidence = params.evidence;
      if (params.acknowledged?.length) fields.acknowledged = params.acknowledged;
      if (params.operator_steps) fields.operator_steps = params.operator_steps;
      return text(await teamCall("report", fields, RECORDED, REPORT_HOLD_MS, signal));
    },
  });

  pi.registerTool({
    name: "AskOrchestrator",
    label: "Ask the orchestrator",
    description:
      `Ask your project's orchestrator a question you cannot settle yourself. The call waits up to ${Math.round(ASK_HOLD_MS / 60_000) || 1} ` +
      "minutes for the answer. If the result says the answer is not in yet, or that it will come as a message, end your " +
      "turn: the answer arrives as your next message. Give the options you see when there are some, and the context the " +
      "orchestrator needs to decide without reading your whole session.",
    promptSnippet: "Ask your project's orchestrator a question you cannot settle yourself",
    promptGuidelines: ["Use AskOrchestrator, never the person at the terminal, for a decision the brief does not make."],
    parameters: Type.Object({
      question: Type.String({ description: "The question, in one or two sentences." }),
      options: Type.Optional(Type.Array(Type.String(), { description: "The choices you see, if any." })),
      context: Type.Optional(Type.String({ description: "What the orchestrator needs to know to decide." })),
    }),
    async execute(_id, params, signal) {
      const fields: Json = { question: params.question, options: params.options ?? [] };
      if (params.context?.trim()) fields.context = params.context;
      return text(await teamCall("ask", fields, EXPIRED, ASK_HOLD_MS, signal));
    },
  });

  for (const set of TOOL_SETS) {
    const file = readSet(set);
    if (!file) continue;
    const unanswered = file.unanswered || "Daedalus did not answer in time; look at the result before trying again.";
    for (const tool of file.tools ?? []) {
      if (!tool?.name || !tool.inputSchema) continue;
      pi.registerTool({
        name: tool.name,
        label: tool.name,
        description: tool.description,
        // The host's own JSON schema, as the MCP route gives it to the other CLIs.
        parameters: Type.Unsafe<Json>(tool.inputSchema),
        async execute(_id, params, signal) {
          return text(await setCall(set, tool.name, (params ?? {}) as Json, unanswered, TOOLS_HOLD_MS, signal));
        },
      });
    }
    postJson("tools", { set, tool: "hello", stage: "extension", client: { name: "pi" } }, 0).catch(() => undefined);
  }

  function startSocket(): void {
    if (server || !DIAL_DIR) return;
    socketPath = join(DIAL_DIR, "pi.sock");
    try {
      unlinkSync(socketPath);
    } catch {
      // Nothing left over from an earlier session.
    }
    server = createServer(serve);
    // A socket that cannot listen (a path past the 107 bytes a unix socket takes, a directory that
    // went) leaves the host unable to send: said once, so the host reports why instead of waiting.
    server.on("error", (error: Error) => emit("bridge_error", { error: error.message.slice(0, 300) }));
    server.listen(socketPath);
  }

  function stopSocket(): void {
    server?.close();
    server = undefined;
    if (socketPath) {
      try {
        unlinkSync(socketPath);
      } catch {
        // Already gone.
      }
    }
  }

  function serve(socket: Socket): void {
    let buffer = "";
    socket.setEncoding("utf8");
    socket.on("error", () => undefined);
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      if (buffer.length > LINE_MAX && !buffer.includes("\n")) {
        buffer = "";
        return;
      }
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.trim()) socket.write(`${JSON.stringify(operate(line))}\n`);
        newline = buffer.indexOf("\n");
      }
    });
  }

  function operate(line: string): Json {
    let request: Json;
    try {
      request = JSON.parse(line) as Json;
    } catch {
      return { ok: false, error: "not JSON" };
    }
    const ctx = context;
    if (!ctx) return { ok: false, error: "the session has not started" };
    try {
      if (request.op === "send") {
        const id = String(request.id ?? "");
        const words = String(request.text ?? "");
        if (!words.trim()) return { ok: false, id, error: "empty message" };
        const idle = ctx.isIdle();
        // Idle, a message starts a turn; busy, pi needs to be told how to queue it: a steer goes in
        // after the running tool calls, anything else waits for the turn to end.
        const deliverAs = idle ? undefined : request.deliverAs === "steer" ? "steer" : "followUp";
        pending.push([id, words]);
        pi.sendUserMessage(words, deliverAs ? { deliverAs } : undefined);
        return { ok: true, id, delivered: deliverAs ?? "prompt" };
      }
      if (request.op === "abort") {
        const idle = ctx.isIdle();
        if (!idle) ctx.abort();
        return { ok: true, wasIdle: idle };
      }
      if (request.op === "state") {
        return { ok: true, idle: ctx.isIdle(), pending: ctx.hasPendingMessages() };
      }
      if (request.op === "shutdown") {
        // pi's own graceful exit, deferred by pi until it is idle: nothing is typed into a TUI
        // that may be showing something else.
        ctx.shutdown();
        return { ok: true };
      }
      return { ok: false, error: `unknown op ${String(request.op)}` };
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) };
    }
  }
}
