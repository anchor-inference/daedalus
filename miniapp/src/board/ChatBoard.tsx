// A chat's own board, in the chat's right panel: the tasks it and its helpers created, stacked by where
// they stand, the way to add one for the agent, and a card opened in place of the list. The panel is
// narrow, so the open card is a view pushed over the list with a way back, not a second column; the
// Board page has the wide form of the same cards.

import { useEffect, useState, type FormEvent } from "react";
import { Icon } from "../icons";
import { t } from "../i18n";
import { pathFor } from "../router";
import { invalidate, useOffline } from "../store";
import { errorText } from "../ui";
import { IconButton, type MenuItem } from "../ui/primitives";
import { BoardColumns, stackedColumns } from "./BoardColumns";
import { closeCard } from "./operatorClose";
import { TaskPane } from "./TaskPane";
import { byUrgency, columnLabel, isFinished, movesFor, type BoardTask } from "./tasks";
import { useChats } from "./useChats";
import { useCreateTask } from "./useCreateTask";
import { useTaskWrites } from "./useTaskWrites";

/** How much of a board is done: finished cards (done or dropped) of all of them, and how many are still open. */
export function boardTally(tasks: readonly { status: string }[] | undefined): { done: number; all: number; open: number; percent: number } {
  const all = tasks?.length ?? 0;
  const done = (tasks ?? []).filter(isFinished).length;
  return { done, all, open: all - done, percent: all ? Math.round((100 * done) / all) : 0 };
}

/** What the Overview's Board row says the chat is on: the task in work (the most urgent one), else the
 *  one up for review, else the next to do; "all finished" once nothing is open. */
export function boardNow(tasks: readonly BoardTask[] | undefined): string {
  const open = (tasks ?? []).filter((task) => !isFinished(task));
  const pick = (status: string) => open.filter((task) => task.status === status).sort(byUrgency)[0];
  const now = pick("doing") ?? pick("review") ?? pick("todo") ?? pick("blocked");
  return now ? t("chatboard.now", { title: now.title }) : t("chatboard.alldone");
}

export type ChatBoardProps = {
  sessionId: string;
  /** The chat's listing, or undefined until it has been read. */
  tasks: BoardTask[] | undefined;
  /** The chat's project, whose board a new card goes on: every chat has one, if only the scratch project made for it. */
  projectId: string;
  /** Set on a helper's chat (an empty string when the leader is unnamed): the leader whose board the host answers with. */
  leaderTitle?: string | null;
  /** The card open in place of the list, or null. */
  taskId: string | null;
  onTask: (id: string | null) => void;
  onOpenSession: (sessionId: string) => void;
  toast: (text: string) => void;
};

export function ChatBoard({ sessionId, tasks, projectId, leaderTitle, taskId, onTask, onOpenSession, toast }: ChatBoardProps) {
  const chats = useChats().info;
  const reload = () => invalidate("/api/board");
  const writes = useTaskWrites(reload);
  const open = taskId ? tasks?.find((task) => task.id === taskId) ?? null : null;
  // A link to a card this chat's board does not hold (removed, or another chat's) falls back to the list.
  useEffect(() => {
    if (taskId && tasks && !open) onTask(null);
  }, [taskId, tasks, open, onTask]);
  if (open) {
    const leave = () => { writes.clear(); onTask(null); };
    return (
      <TaskPane task={open} chats={chats} writes={writes} here={sessionId} others={tasks} toast={toast}
        back={{ label: t("chatboard.back"), onBack: leave }} onClose={leave} onChanged={reload}
        onOpenSession={onOpenSession} onOpenTask={onTask} onRemoved={leave} />
    );
  }
  const helper = leaderTitle !== null && leaderTitle !== undefined;
  const tally = boardTally(tasks);
  const columns = stackedColumns(tasks ?? []).filter((column) => column.tasks.length > 0);
  const cardMenu = (task: BoardTask): MenuItem[] => {
    const blocked = writes.blocked(task);
    const moves = movesFor(task).map((status) => ({
      label: t("board.move.to", { name: columnLabel(status) }), icon: "forward" as const, disabled: blocked,
      onSelect: () => void writes.move(task, status, "card"),
    }));
    // A close asks its own route; its refusal is written on the card, where the menu was opened.
    const hooks = { onDone: reload, toast: (text: string) => writes.refuse(task, "card", text) };
    const closes: MenuItem[] = isFinished(task) ? [] : [
      { label: t("pboard.close.done.action"), icon: "check", disabled: blocked, onSelect: () => void closeCard(task, "done", hooks) },
      { label: t("pboard.close.dropped.action"), icon: "archive", disabled: blocked, onSelect: () => void closeCard(task, "dropped", hooks) },
    ];
    return [...moves, ...(moves.length && closes.length ? ["-" as const] : []), ...closes];
  };
  return (
    <div className="cb" data-tool-page="board">
      <div className="cb-head">
        <div className="cb-head-main">
          <div className="cb-title">
            <span>{helper ? (leaderTitle ? t("chatboard.title.helper", { leader: leaderTitle }) : t("chatboard.title.leader")) : t("chatboard.title")}</span>
            {tally.all > 0 && <span className="cb-count">{t("chatboard.count", { done: tally.done, all: tally.all })}</span>}
          </div>
          {helper && <div className="cb-sub">{t("chatboard.helper")}</div>}
          {tally.all > 0 && <div className="cb-bar" role="img" aria-label={t("chatboard.bar", { n: tally.percent })}><i style={{ width: `${tally.percent}%` }} /></div>}
        </div>
        <IconButton icon="external" label={t("chatboard.full")} href={pathFor("board", null, { scope: "chat", session: sessionId })} />
      </div>
      <div className="cb-scroll">
        {tasks === undefined && <div className="tool-empty">{t("common.loading")}</div>}
        {tasks !== undefined && tasks.length === 0 && <div className="tool-empty" data-empty>{t("chatboard.empty")}</div>}
        {tasks !== undefined && tasks.length > 0 && (
          <BoardColumns columns={columns} scope="chat" layout="stacked" chats={chats} onOpen={(task) => onTask(task.id)}
            canMove={(task) => (writes.blocked(task) ? [] : movesFor(task))} onMove={(task, status) => void writes.move(task, status, "card")}
            refusal={writes.refusal} cardMenu={cardMenu} />
        )}
      </div>
      <AddTask sessionId={sessionId} projectId={projectId} onAdded={reload} />
    </div>
  );
}

/** The field under the list: a line of words becomes a card in To do whose origin is this chat, which
 *  the agent finds among its own tasks. Its refusal is written under the field, where the words were typed. */
function AddTask({ sessionId, projectId, onAdded }: { sessionId: string; projectId: string; onAdded: () => void }) {
  const offline = useOffline();
  const createTask = useCreateTask();
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const words = title.trim();
    if (!words || busy) return;
    if (offline) { setRefused(t("result.block.unconfirmed")); return; }
    setBusy(true);
    try {
      await createTask(projectId, { title: words, session_id: sessionId });
      setTitle("");
      setRefused(null);
      onAdded();
    } catch (e) {
      setRefused(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="cb-add-wrap" onSubmit={(e) => void submit(e)}>
      <label className={`cb-add ${refused ? "refused" : ""}`}>
        <Icon name="plus" size={16} />
        <input type="text" value={title} maxLength={200} enterKeyHint="send" disabled={busy} placeholder={t("chatboard.add")} aria-label={t("chatboard.add")}
          onChange={(e) => { setTitle(e.target.value); if (refused) setRefused(null); }} />
      </label>
      {refused && <div className="task-refusal cb-add-refusal" role="alert">{refused}</div>}
    </form>
  );
}
