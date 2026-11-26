// The main orchestrator's entry: first in orchestration mode's column, pinned above a project's focus
// column, and first in the list on a phone. It says whether the main
// orchestrator is at work and how many questions wait in its chat, so the operator sees from anywhere
// that something needs them there.

import type { SessionList } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { go } from "../shell";
import { useQuery } from "../store";
import { useStreamUp } from "../events";
import { useMain } from "./data";
import { MAIN_PATH } from "./model";

export function MainEntry({ current }: { current: boolean }) {
  const { data } = useMain();
  const live = useStreamUp();
  // The list the sidebar already reads says whether the session is at work; no second request.
  const sessions = useQuery<SessionList>("/api/sessions", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const row = data?.session_id ? sessions.data?.sessions.find((s) => s.id === data.session_id) : undefined;
  const working = row?.status === "running";
  const questions = data?.questions ?? 0;
  const going = (data?.dispatches ?? []).filter((d) => d.status === "open" || d.status === "blocked").length;
  const label = t("main.title");
  // One 32 px line, the Agents column's row: the questions that wait are its state word in the
  // attention colour, and otherwise whether it works or how many dispatches are under way.
  const sub = working ? t("main.entry.working") : going ? plural("main.entry.going", going) : "";
  return (
    <a className={`sb-row main-entry ${current ? "current" : ""} ${working ? "working" : ""}`} href={MAIN_PATH} onClick={(e) => go(e, MAIN_PATH)} aria-current={current ? "page" : undefined} title={working || going ? `${label} · ${sub}` : `${label} · ${t("main.entry.sub")}`} data-main-entry data-nav="main">
      <span className="sb-tile"><Icon name="compass" size={16} /></span>
      <span className="sb-t main-entry-name">{label}</span>
      {questions > 0
        ? <span className="sb-word waiting main-pill" data-questions={questions}>{plural("main.questions", questions)}</span>
        : sub && <span className={`main-entry-sub ${working ? "sb-word running" : "sb-time"}`}>{sub}</span>}
    </a>
  );
}
