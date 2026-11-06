// The extension's side panel: the app at /app/panel, framed by the Daedalus extension in Chrome.
//
// It shows the conversations that use this Chrome and nothing else, following the window's active
// tab: the conversation whose tab group holds that tab, or the one shown last, or the newest. The
// extension's page around it lends what only an extension can do (the current tab, a picture of it,
// its text, the selection, giving the tab to a conversation, showing the agent's tab, pausing the
// agent's group) through the bridge; everything else is the app's own pieces — the transcript, the
// composer with its model and mode pickers and the agent's questions — talking to the panel's door
// with the panel's credential.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api, setPanelCredential, type AsrStatus, type SessionDetail } from "../api";
import { applyAppearance } from "../appearance";
import { ApprovalChip, type ApprovalMode } from "../browser/approvals";
import { type Approval, type QueuedMessage, pendingApproval, queuedAfter, readQueued, readReturned } from "../composer";
import { FilePreview, type PreviewSource } from "../preview";
import { Composer, type ComposerHandle } from "../composerbox";
import type { ModelChoice } from "../modelselect";
import { messageBody } from "../project/goalmodel";
import { Icon } from "../icons";
import { num, plural, setLang, t, useLang } from "../i18n";
import { sessionPath } from "../router";
import { onMicRefused } from "../voicebar";
import { QuestionsPanel } from "../questions/QuestionsPanel";
import { useQuestions } from "../questions/data";
import { questionsKey } from "../questions/routes";
import { invalidate } from "../store";
import { LiveBar, TranscriptView } from "../screens/Session";
import { buildTurns, createLiveStore, liveAfter, liveBase, type LiveStore, type Turn } from "../turns";
import { followSession } from "../sessionstream";
import { errorText } from "../ui";
import { ToastHost, toast } from "../ui/dialogs";
import { connectShell, ShellError, type Current, type PageInfo, type Shell } from "./bridge";
import { cardKey, cropRect, dataFile, lineStops, pageFile, panelCarries, pickSession, selectionQuote, stopUnconfirmed, type PanelListing, type PanelSession, type Rect } from "./model";
import "./sidepanel.css";

type Status = "idle" | "running" | "waiting" | "failed" | "compacting";

/** What the extension asks the panel to show when the operator chose "Ask Daedalus about this page". */
const NEW = "new";
function statusOf(detail: SessionDetail | null): Status {
  const status = detail?.status ?? "idle";
  return (["running", "waiting", "failed", "compacting"].includes(status) ? status : "idle") as Status;
}

export function SidePanel() {
  useLang();
  const [shell, setShell] = useState<Shell | null>(null);
  const [outside, setOutside] = useState(false);
  const [current, setCurrent] = useState<Current | null>(null);
  const [listing, setListing] = useState<PanelListing | null>(null);
  const [shown, setShown] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const [creating, setCreating] = useState(false);
  const [menu, setMenu] = useState(false);
  const show = useRef<string | null>(null);
  // The actions answered here, per conversation, for as long as the panel is open: the refusal stays
  // in the transcript after the answer, and coming back to the conversation must not offer it again.
  const answered = useRef(new Map<string, Set<string>>());

  useEffect(() => {
    if (!shell) return;
    onMicRefused(() => {
      // The extension opens its own page, where Chrome asks once; the panel's frame then has it.
      shell.request("mic.grant").then(() => toast(t("sidepanel.mic.grant"))).catch((e) => toast(t("sidepanel.tab.failed", { reason: shellReason(e) })));
    });
    return () => onMicRefused(null);
  }, [shell]);

  useEffect(() => {
    applyAppearance();
    let gone = false;
    connectShell()
      .then((found) => {
        if (gone) return;
        setPanelCredential(found.hello.token, refresher(found));
        if (found.hello.lang === "ru" || found.hello.lang === "en") setLang(found.hello.lang);
        // "new" is the page's menu, "Ask Daedalus about this page": a new conversation for that page.
        if (found.hello.show === NEW) setCreating(true);
        else show.current = found.hello.show;
        setCurrent(found.hello.current);
        setShell(found);
      })
      .catch((error) => !gone && error instanceof ShellError && setOutside(true));
    return () => {
      gone = true;
    };
  }, []);

  const loadList = useCallback(async () => {
    try {
      setListing(await api.get<PanelListing>("/api/panel/sessions"));
    } catch (e) {
      toast(errorText(e));
    }
  }, []);

  useEffect(() => {
    if (!shell) return;
    void loadList();
    const timer = window.setInterval(() => void loadList(), 15000);
    const off = [
      shell.on("current", (value: Current) => setCurrent(value)),
      shell.on("token", (value: { token: string }) => value?.token && setPanelCredential(value.token, refresher(shell))),
      shell.on("show", (session: string) => {
        if (session === NEW) {
          setCreating(true);
          return;
        }
        show.current = session;
        setCreating(false);
        void loadList();
      }),
      shell.on("event", () => void loadList()),
    ];
    return () => {
      window.clearInterval(timer);
      for (const stop of off) stop();
    };
  }, [shell, loadList]);

  const sessions = listing?.sessions ?? [];
  // Which conversation to show follows the tab, the list and a notification's click.
  useEffect(() => {
    if (!listing) return;
    const next = pickSession({ show: show.current, current, sessions, shown, pinned });
    if (show.current && next === show.current) show.current = null;
    if (next !== shown) setShown(next);
  }, [listing, current, pinned]); // eslint-disable-line react-hooks/exhaustive-deps

  if (outside) {
    return (
      <div className="sidepanel sp-outside">
        <Icon name="logo" size={28} />
        <p>{t("sidepanel.outside")}</p>
      </div>
    );
  }
  if (!shell || !listing) {
    return <div className="sidepanel sp-loading" aria-busy="true"><div className="skeleton sk-line" /></div>;
  }

  const session = sessions.find((s) => s.id === shown) ?? null;
  const newView = creating || !session;

  return (
    <div className="sidepanel">
      <header className="sp-head">
        <button type="button" className="sp-title" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((open) => !open)} disabled={!sessions.length}>
          {session && !newView ? <Dot status={session.status} needsYou={session.needs_you} /> : null}
          <span className="truncate">{newView ? t("sidepanel.new.title") : session.title}</span>
          {sessions.length > 0 && <Icon name="chevron" size={14} />}
        </button>
        <span className="grow" />
        {!newView && (
          <button type="button" className={`iconbtn small ${pinned ? "on" : ""}`} title={t(pinned ? "sidepanel.unpin" : "sidepanel.pin")} aria-label={t(pinned ? "sidepanel.unpin" : "sidepanel.pin")} aria-pressed={pinned} onClick={() => setPinned((p) => !p)}>
            <Icon name="pushpin" size={16} />
          </button>
        )}
        <button type="button" className="iconbtn small" title={t("sidepanel.new")} aria-label={t("sidepanel.new")} onClick={() => { setCreating(true); setMenu(false); }}>
          <Icon name="compose" size={16} />
        </button>
        <button type="button" className="iconbtn small" title={t("sidepanel.open_app")} aria-label={t("sidepanel.open_app")} onClick={() => void shell.request("open.app", { path: session && !newView ? sessionPath(session.id) : "/app/" }).catch((e) => toast(errorText(e)))}>
          <Icon name="external" size={16} />
        </button>
      </header>
      {menu && (
        <div className="sp-menu" role="menu">
          {sessions.map((s) => (
            <button key={s.id} type="button" role="menuitem" className={`sp-menu-row ${s.id === shown && !newView ? "on" : ""}`}
              onClick={() => { setShown(s.id); setCreating(false); setMenu(false); setPinned(true); }}>
              <Dot status={s.status} needsYou={s.needs_you} />
              <span className="truncate grow">{s.title}</span>
              {s.needs_you && <span className="sp-badge">{t("sidepanel.needs_you")}</span>}
            </button>
          ))}
        </div>
      )}
      {!listing.chrome.connected && <div className="sp-banner warn">{t("sidepanel.chrome.offline")}</div>}
      {newView ? (
        <NewConversation shell={shell} current={current} listing={listing} toast={toast}
          onCreated={async (id) => { await loadList(); setShown(id); setCreating(false); setPinned(false); }}
          onCancel={sessions.length ? () => setCreating(false) : undefined} />
      ) : (
        <Conversation key={session.id} session={session} shell={shell} current={current} toast={toast} onChanged={loadList} answered={answered.current} />
      )}
      <ToastHost />
    </div>
  );
}

/** How the API client asks the extension for a newer credential when the one in hand stops working. */
function refresher(shell: Shell): () => Promise<string | null> {
  return async () => {
    const fresh = await shell.request<{ token: string } | null>("token.refresh");
    return fresh?.token ?? null;
  };
}

function Dot({ status, needsYou }: { status: string; needsYou: boolean }) {
  const tone = needsYou ? "waiting" : status === "running" || status === "compacting" ? "running" : status === "failed" ? "failed" : "idle";
  return <span className={`sp-dot ${tone}`} aria-hidden />;
}

// ── a new conversation ─────────────────────────────────────────────────────────────────────

/** What a new conversation's composer holds until the conversation exists: the model, the effort and
 *  the approval mode the operator picked, applied right after it is made and before the first message. */
type NewChoices = { model: ModelChoice | null; label: string; effort: string | null; approvals: ApprovalMode };

/**
 * Starts a conversation in this Chrome, as the app's own "new chat" does (the default model, no
 * project), with the app's whole composer: dictation, files and pictures (picked, dropped or pasted),
 * the model and its effort, the approval mode, and the page's text, a screenshot or the selection. It
 * gives the conversation the tab in front of the operator at once: sending here is their explicit act.
 * The first time, the consent summary stands above the composer, and the first message is the yes.
 */
function NewConversation({ shell, current, listing, toast, onCreated, onCancel }: { shell: Shell; current: Current | null; listing: PanelListing; toast: (text: string) => void; onCreated: (id: string) => Promise<void>; onCancel?: () => void }) {
  const composer = useRef<ComposerHandle>(null);
  const tab = current?.tab?.readable ? current.tab : null;
  const asr = useAsr();
  const settings = usePanelSettings();
  const [choices, setChoices] = useState<NewChoices>({ model: null, label: "", effort: null, approvals: "manual" });
  const { attach, shot, setShot } = useAttach(shell, composer, toast);
  useEffect(() => composer.current?.focus(), []);

  async function send(text: string, files: File[]): Promise<"sent" | "queued"> {
    const words = text.trim() || files.map((file) => file.name).join(", ");
    const made = await api.post<{ id: string; group: string; title: string }>("/api/panel/sessions", { title: words.slice(0, 400), consent: !listing.consented });
    // What was picked before the conversation existed lands before its first message, so the first
    // turn already runs on that model and under that approval mode.
    if (choices.model) await api.post(`/api/sessions/${made.id}/model`, choices.model).catch((e) => toast(errorText(e)));
    if (choices.effort) await api.post(`/api/sessions/${made.id}/model`, choices.effort === "off" ? { thinking: false } : { thinking: true, reasoning_effort: choices.effort }).catch((e) => toast(errorText(e)));
    if (choices.approvals !== "manual") {
      await api.put(`/api/sessions/${made.id}/browser-approvals`, { mode: choices.approvals, confirm: choices.approvals === "skip" }).catch((e) => toast(errorText(e)));
    }
    let gave = false;
    if (tab) {
      try {
        await shell.request("tab.grant", { group: made.group, session: made.id, title: made.title || words.slice(0, 80) });
        await api.post(`/api/panel/sessions/${made.id}/tab`);
        gave = true;
      } catch (e) {
        toast(t("sidepanel.tab.failed", { reason: shellReason(e) }));
      }
    }
    const said = gave && tab ? `${text.trim()}\n\n${t("sidepanel.message.tab", { title: tab.title || tab.url, url: tab.url })}`.trim() : text.trim();
    if (files.length) {
      const form = new FormData();
      form.append("text", said);
      form.append("client_message_id", crypto.randomUUID());
      form.append("expected_running", "false");
      for (const file of files) form.append("files", file, file.name);
      await api.upload(`/api/sessions/${made.id}/upload`, form);
    } else {
      await api.post(`/api/sessions/${made.id}/messages`, messageBody(said, { clientMessageId: crypto.randomUUID() }));
    }
    await onCreated(made.id);
    return "sent";
  }

  // A preset's label may be empty (one added from a provider's list is named by its model), and
  // `??` kept the empty string: the pill then read the placeholder word "model" instead of a name.
  const presetName = (id: string) => {
    const p = settings?.presets[id];
    return p ? p.label || `${p.provider}/${p.model}` : id;
  };
  const defaultLabel = settings ? presetName(settings.model.preset) : "";
  return (
    <div className="sp-new" onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
      onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); composer.current?.addFiles(e.dataTransfer.files); } }}>
      <div className="sp-new-intro">
        {!listing.consented && (
          <div className="sp-consent">
            <b>{t("sidepanel.consent.title")}</b>
            <p>{listing.consent}</p>
            <p className="sub">{t("sidepanel.consent.send")}</p>
          </div>
        )}
        <div className="sp-new-tab">
          {tab ? <><Icon name="globe" size={14} /><span className="truncate">{t("sidepanel.new.tab", { title: tab.title || tab.url })}</span></> : <span className="sub">{t("sidepanel.new.notab")}</span>}
          {onCancel && <button type="button" className="link sp-new-cancel" onClick={onCancel}>{t("sidepanel.cancel")}</button>}
        </div>
      </div>
      {shot && <ShotCrop shot={shot} onCancel={() => setShot(null)} onAttach={(file) => { composer.current?.addFiles([file]); setShot(null); }} />}
      <Composer
        ref={composer}
        above={<AttachChips tab={tab} attach={attach} />}
        beside={<ApprovalChip sessionId={null} value={choices.approvals} onPick={(approvals) => setChoices((c) => ({ ...c, approvals }))} toast={toast} />}
        sessionId=""
        status="idle"
        idlePlaceholder={t("sidepanel.new.placeholder")}
        onSend={(text, files) => send(text, files)}
        onStop={() => {}}
        commands={[]}
        onCommand={async () => toast(t("sidepanel.commands.app"))}
        model={choices.label || defaultLabel}
        fallback={null}
        onChooseModel={async (choice) => {
          const label = "preset" in choice ? presetName(choice.preset) : "model" in choice ? choice.model : "";
          setChoices((c) => ({ ...c, model: "clear" in choice ? null : choice, label }));
        }}
        thinking={choices.effort !== "off"}
        reasoningEffort={choices.effort && choices.effort !== "off" ? choices.effort : undefined}
        onChooseEffort={async (effort) => setChoices((c) => ({ ...c, effort }))}
        queued={[]}
        questions={null}
        answered={null}
        onAnswer={async () => {}}
        approval={null}
        asr={asr}
        phone={false}
        toast={toast}
      />
    </div>
  );
}

/** The page's own offers over the composer: its text as a file, a screenshot, the selection quoted. */
function AttachChips({ tab, attach, asks }: { tab: { readable?: boolean } | null; attach: (kind: "page" | "screenshot" | "selection") => void; asks?: { count: number; open: boolean; toggle: () => void } }) {
  return (
    <div className="sp-attach" role="group" aria-label={t("sidepanel.attach")}>
      <button type="button" className="chip" disabled={!tab?.readable} onClick={() => attach("page")}><Icon name="file" size={13} />{t("sidepanel.attach.page")}</button>
      <button type="button" className="chip" disabled={!tab?.readable} onClick={() => attach("screenshot")}><Icon name="camera" size={13} />{t("sidepanel.attach.screenshot")}</button>
      <button type="button" className="chip" disabled={!tab?.readable} onClick={() => attach("selection")}><Icon name="reply" size={13} />{t("sidepanel.attach.selection")}</button>
      {/* The agent's waiting questions, at the end of the row: the list over the composer folds away
          and this brings it back, so hiding it never means losing it. */}
      {asks && asks.count > 0 && (
        <button type="button" className={`chip sp-asks-chip ${asks.open ? "on" : ""}`} onClick={asks.toggle} aria-expanded={asks.open} title={plural("questions.line", asks.count)}>
          <Icon name="ask" size={13} />{num(asks.count)}
        </button>
      )}
    </div>
  );
}

/** Taking the page into the composer, for a new conversation and an open one alike. */
function useAttach(shell: Shell, composer: React.RefObject<ComposerHandle | null>, toast: (text: string) => void) {
  const [shot, setShot] = useState<(PageInfo & { image: string }) | null>(null);
  const attach = useCallback(async (kind: "page" | "screenshot" | "selection") => {
    try {
      if (kind === "page") {
        const page = await shell.request<PageInfo & { text: string }>("page.text");
        composer.current?.addFiles([pageFile(page)]);
      } else if (kind === "selection") {
        const picked = await shell.request<PageInfo & { text: string }>("page.selection");
        if (!picked.text.trim()) toast(t("sidepanel.attach.noselection"));
        else composer.current?.insert(selectionQuote(picked));
      } else {
        setShot(await shell.request<PageInfo & { image: string }>("capture.visible"));
      }
    } catch (e) {
      toast(t("sidepanel.attach.failed", { reason: shellReason(e) }));
    }
  }, [shell, composer, toast]);
  return { attach: (kind: "page" | "screenshot" | "selection") => void attach(kind), shot, setShot };
}

/** Whether dictation is offered: the app's own answer, through the panel's door. */
function useAsr(): AsrStatus | null {
  const [asr, setAsr] = useState<AsrStatus | null>(null);
  useEffect(() => {
    let gone = false;
    api.get<AsrStatus>("/api/asr").then((a) => { if (!gone) setAsr(a); }).catch(() => { if (!gone) setAsr(null); });
    return () => { gone = true; };
  }, []);
  return asr;
}

type PanelSettings = { presets: Record<string, { label?: string; provider?: string; model?: string }>; model: { preset: string } };

/** The model list's default, for a conversation that does not exist yet. */
function usePanelSettings(): PanelSettings | null {
  const [found, setFound] = useState<PanelSettings | null>(null);
  useEffect(() => {
    let gone = false;
    api.get<PanelSettings>("/api/settings").then((s) => { if (!gone) setFound(s); }).catch(() => {});
    return () => { gone = true; };
  }, []);
  return found;
}

function shellReason(e: unknown): string {
  const code = e instanceof Error ? e.message : String(e);
  if (code === "gesture") return t("sidepanel.error.gesture");
  if (code === "unreadable") return t("sidepanel.error.unreadable");
  return code;
}

// ── one conversation ───────────────────────────────────────────────────────────────────────

function Conversation({ session, shell, current, toast, onChanged, answered }: { session: PanelSession; shell: Shell; current: Current | null; toast: (text: string) => void; onChanged: () => void; answered: Map<string, Set<string>> }) {
  const id = session.id;
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [seenKeys, setSeenKeys] = useState<Set<string>>(() => answered.get(id) ?? new Set());
  useEffect(() => void answered.set(id, seenKeys), [answered, id, seenKeys]);
  const [similar, setSimilar] = useState<string | null>(null);
  const [stopAt, setStopAt] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const composer = useRef<ComposerHandle>(null);
  const { attach, shot, setShot } = useAttach(shell, composer, toast);
  const asr = useAsr();
  const scroller = useRef<HTMLDivElement>(null);
  const liveRef = useRef<LiveStore | null>(null);
  if (!liveRef.current) liveRef.current = createLiveStore();
  const live = liveRef.current;

  const load = useCallback(async () => {
    try {
      setDetail(await api.get<SessionDetail>(`/api/sessions/${id}?tail=200`));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [id]);
  // The messages waiting for the running agent. The panel passed an empty list, so a message sent
  // during a turn vanished from the panel until the agent read it, while the app showed its card.
  const [queued, setQueued] = useState<QueuedMessage[]>([]);
  const sideAsked = useQuestions({ sessionId: id }).questions?.length ?? 0;
  // The list opens by itself when a new question arrives, and the chip folds it away or back.
  const [asksOpen, setAsksOpen] = useState(true);
  const seenAsked = useRef(sideAsked);
  useEffect(() => {
    if (sideAsked > seenAsked.current) setAsksOpen(true);
    seenAsked.current = sideAsked;
  }, [sideAsked]);
  const loadQueued = useCallback(async () => {
    try {
      setQueued(readQueued(await api.get<unknown>(`/api/sessions/${id}/steer`)));
    } catch {
      setQueued([]);
    }
  }, [id]);
  const takeBack = useCallback(
    async (message: QueuedMessage, edit: boolean) => {
      try {
        const r = await api.delete<{ message?: unknown }>(`/api/sessions/${id}/steer/${encodeURIComponent(message.id)}`);
        setQueued((q) => q.filter((x) => x.id !== message.id));
        if (edit) composer.current?.insert((readReturned([r?.message])[0] ?? message).text);
      } catch (e) {
        toast(errorText(e));
        void loadQueued();
      }
    },
    [id, loadQueued],
  );
  const steerNow = useCallback(
    async (message: QueuedMessage) => {
      try {
        await api.post(`/api/sessions/${id}/steer/${encodeURIComponent(message.id)}`, {});
        setQueued((q) => q.map((x) => (x.id === message.id ? { ...x, kind: "steer" } : x)));
      } catch (e) {
        toast(errorText(e));
        void loadQueued();
      }
    },
    [id, loadQueued],
  );

  useEffect(() => {
    void load();
    void loadQueued();
    let soon: number | null = null;
    const stop = followSession(id, {
      handle: (event, p) => {
        live.update((state) => liveAfter(state, event, p));
        if (event === "steer_changed") setQueued((q) => queuedAfter(q, p));
        // The panel has no app-wide event stream: a question the agent asks is read again when its
        // step ends rather than at the next ten-second poll.
        if (event === "message_stop" || event === "run_settled") invalidate(questionsKey({ sessionId: id }));
        if (event === "message_start") setDetail((prev) => (prev && prev.status !== "running" ? { ...prev, status: "running", run_id: p.run_id || prev.run_id } : prev));
        if (event === "run_settled") {
          setDetail((prev) => (prev ? { ...prev, status: p.status === "awaiting" ? "waiting" : p.status === "failed" ? "failed" : "idle" } : prev));
          onChanged();
        }
        if (["message_start", "message_stop", "run_settled", "compaction_completed", "history_cut"].includes(event) && soon === null) {
          soon = window.setTimeout(() => {
            soon = null;
            void load();
          }, 150);
        }
      },
      onDrop: () => {
        live.reset();
        void load();
        void loadQueued();
      },
    });
    return () => {
      stop();
      if (soon !== null) window.clearTimeout(soon);
      live.reset();
    };
  }, [id, live, load, loadQueued, onChanged]);

  const status = statusOf(detail);
  const busy = status === "running" || status === "waiting" || status === "compacting";
  useEffect(() => {
    if (!busy) live.reset();
  }, [busy, live]);
  useEffect(() => {
    if (status !== "running" && status !== "compacting") setStopAt(null);
  }, [status]);
  useEffect(() => {
    if (stopAt === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [stopAt]);

  const follow = useCallback(() => {
    const el = scroller.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 200) el.scrollTop = el.scrollHeight;
  }, []);
  useEffect(() => {
    const el = scroller.current;
    if (el && detail) el.scrollTop = el.scrollHeight;
  }, [detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Offered only while the host lists the request as open, as on the app's own screen: read from the
  // transcript alone, a request answered anywhere came back on every reload of the panel.
  const approval = useMemo(
    () => pendingApproval(detail?.messages ?? [], seenKeys, { open: detail?.open_approvals, runId: detail?.run_id }),
    [detail?.messages, seenKeys, detail?.open_approvals, detail?.run_id],
  );
  useEffect(() => {
    setSimilar(null);
    if (!approval) return;
    let gone = false;
    api.get<{ similar?: { label?: unknown } | null }>(`/api/sessions/${id}/policy/similar/${approval.key}`)
      .then((r) => !gone && typeof r?.similar?.label === "string" && setSimilar(r.similar.label))
      .catch(() => undefined);
    return () => {
      gone = true;
    };
  }, [id, approval?.key]); // eslint-disable-line react-hooks/exhaustive-deps

  async function decide(a: Approval, how: "allow" | "similar" | "deny") {
    setSeenKeys((keys) => new Set(keys).add(a.key));
    try {
      if (how === "deny") await api.post(`/api/sessions/${id}/policy/refuse`, { key: a.key });
      else if (how === "similar" && similar) await api.post(`/api/sessions/${id}/policy/grant-similar`, { key: a.key });
      else await api.post(`/api/sessions/${id}/policy/grant`, { key: a.key });
      onChanged();
    } catch (e) {
      toast(errorText(e));
    }
  }

  // A message sent while the agent works steers it at its next step (see `midRun` on the composer).
  // It was a follow-up as in the app, and the operator's "only China" waited forty seconds, six model
  // calls, until they pressed Steer on its card by hand. Its card still offers to withdraw it.
  async function send(text: string, files: File[], running: boolean, clientMessageId?: string, onProgress?: (fraction: number) => void): Promise<"sent" | "queued" | "steered"> {
    const messageId = clientMessageId ?? crypto.randomUUID();
    let result: { receipt?: { status: string } };
    if (files.length) {
      const form = new FormData();
      form.append("text", text);
      form.append("client_message_id", messageId);
      if (running) form.append("steer", "true");
      form.append("expected_running", running ? "true" : "false");
      for (const file of files) form.append("files", file, file.name);
      result = await api.upload<{ receipt?: { status: string } }>(`/api/sessions/${id}/upload`, form, onProgress);
    } else {
      result = await api.post<{ receipt?: { status: string } }>(`/api/sessions/${id}/messages`, messageBody(text, { steer: running, clientMessageId: messageId }));
    }
    void load();
    void loadQueued();
    return result.receipt?.status === "queued" ? (running ? "steered" : "queued") : "sent";
  }

  async function stop() {
    setStopAt(Date.now());
    setNow(Date.now());
    try {
      await api.post(`/api/sessions/${id}/stop`);
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function pause(next: boolean, takeOver = false) {
    try {
      await shell.request("group.pause", { session: id, paused: next });
      setPaused(next);
      if (takeOver) {
        await shell.request("tab.focus", { session: id });
      }
    } catch (e) {
      toast(t("sidepanel.tab.failed", { reason: shellReason(e) }));
    }
  }

  async function chooseModel(body: ModelChoice) {
    try {
      await api.post(`/api/sessions/${id}/model`, body);
      void load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function chooseEffort(effort: string) {
    try {
      await api.post(`/api/sessions/${id}/model`, effort === "off" ? { thinking: false } : { thinking: true, reasoning_effort: effort });
      void load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function giveTab() {
    try {
      await shell.request("tab.grant", { group: session.group, session: id, title: session.title });
      await api.post(`/api/panel/sessions/${id}/tab`);
    } catch (e) {
      toast(t("sidepanel.tab.failed", { reason: shellReason(e) }));
    }
  }

  // A file the conversation carries opens here. Anything else the transcript names, a file in the
  // workspace, is behind a door the panel does not have, and the press goes to the app, where it is.
  const [previewing, setPreviewing] = useState<PreviewSource | null>(null);
  const preview = useCallback((src: PreviewSource) => {
    if (panelCarries(src)) setPreviewing(src);
    else void shell.request("open.app", { path: sessionPath(id) }).catch((e) => toast(errorText(e)));
  }, [id, shell]);

  const tab = current?.tab ?? null;
  const tabIsTheirs = current?.owner?.session === id;
  const canGive = !!tab?.readable && !current?.owner;

  return (
    <>
      <div className="sp-body" ref={scroller} onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
        onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); composer.current?.addFiles(e.dataTransfer.files); } }}>
        {detail ? (
          <TranscriptView key={detail.id} detail={detail} live={live} onPreview={preview} toast={toast} onRender={follow} />
        ) : failed ? (
          <div className="empty">{t("sidepanel.unreadable")}</div>
        ) : (
          <div className="skeleton sk-line" />
        )}
      </div>
      {canGive && (
        <div className="sp-give">
          <span className="truncate sub">{tab!.title || tab!.url}</span>
          <button type="button" className="btn small" onClick={() => void giveTab()}>{t("sidepanel.tab.give")}</button>
        </div>
      )}
      {/* The agent's side questions in this conversation. The panel never fetched them, so a question
          asked here waited unseen while only the app showed it. */}
      {sideAsked > 0 && asksOpen && <div className="sp-questions"><QuestionsPanel scope={{ sessionId: id }} toast={toast} /></div>}
      {approval && <ApprovalCard key={approval.key} approval={approval} similar={similar} onDecide={(how) => void decide(approval, how)} />}
      {shot && <ShotCrop shot={shot} onCancel={() => setShot(null)} onAttach={(file) => { composer.current?.addFiles([file]); setShot(null); }} />}
      {previewing && <FilePreview src={previewing} onClose={() => setPreviewing(null)} />}
      {detail?.id === id && (
        <Composer
          ref={composer}
          above={
            <>
            {(busy || paused) && (
              <ActivityLine detail={detail} live={live} status={status} paused={paused} stopUnconfirmed={stopUnconfirmed(stopAt, status, now)} hasTab={tabIsTheirs || !!current?.owner}
                onStop={() => void stop()} onPause={() => void pause(!paused)} onTakeOver={() => void pause(true, true)}
                onShowTab={() => void shell.request("tab.focus", { session: id }).catch((e) => toast(t("sidepanel.tab.failed", { reason: shellReason(e) })))}
                onOpenApp={() => void shell.request("open.app", { path: sessionPath(id) })} />
            )}
            <AttachChips tab={tab} attach={attach} asks={{ count: sideAsked, open: asksOpen, toggle: () => setAsksOpen((o) => !o) }} />
            </>
          }
          beside={<ApprovalChip key={id} sessionId={id} toast={toast} />}
          sessionId={id}
          status={status}
          onSend={send}
          midRun="steer"
          onStop={() => void stop()}
          commands={[]}
          onCommand={async () => toast(t("sidepanel.commands.app"))}
          model={detail.model ?? ""}
          fallback={detail.fallback ?? null}
          onChooseModel={chooseModel}
          thinking={detail.thinking}
          reasoningEffort={detail.reasoning_effort}
          onChooseEffort={chooseEffort}
          queued={queued}
          onWithdraw={(message) => void takeBack(message, false)}
          onEdit={(message) => void takeBack(message, true)}
          onSteer={(message) => void steerNow(message)}
          questions={detail.pending?.questions ?? null}
          answered={detail.pending?.answered ?? null}
          onAnswer={async (answers) => {
            try {
              await api.post(`/api/sessions/${id}/answer`, { answers });
            } finally {
              void load();
            }
          }}
          approval={null}
          asr={asr}
          phone={false}
          toast={toast}
        />
      )}
    </>
  );
}

// ── the activity line ──────────────────────────────────────────────────────────────────────

/** What the agent is doing, as one line at the top of the composer: the dot, the current step cut to
 *  fit, and small buttons for the tabs. It was a bordered block with its own row of labelled buttons
 *  and a red Stop beside the composer's red Stop; in a 360 px panel that read as a second panel. Stop
 *  stays the composer's (see `lineStops`), and the warning that a stop went unanswered sits here,
 *  right over that button. */
function ActivityLine({ detail, live, status, paused, stopUnconfirmed: unconfirmed, hasTab, onStop, onPause, onTakeOver, onShowTab, onOpenApp }: {
  detail: SessionDetail; live: LiveStore; status: Status; paused: boolean; stopUnconfirmed: boolean; hasTab: boolean;
  onStop: () => void; onPause: () => void; onTakeOver: () => void; onShowTab: () => void; onOpenApp: () => void;
}) {
  useSyncExternalStore(live.subscribe, live.get);
  const built = useRef<Turn[]>([]);
  const turns = useMemo(() => {
    built.current = buildTurns(detail.messages ?? [], built.current);
    return built.current;
  }, [detail.messages]);
  const base = liveBase(turns, detail.run_id);
  const resumeLabel = t(paused ? "sidepanel.resume" : "sidepanel.pause");
  return (
    <div className="sp-live-wrap" role="region" aria-label={t("sidepanel.strip")}>
      <div className="sp-live">
        {paused ? (
          <span className="sp-live-paused" role="status" title={t("sidepanel.paused")}><span className="sp-dot waiting" /><span className="truncate">{t("sidepanel.paused.short")}</span></span>
        ) : (
          <LiveBar compact status={status === "compacting" ? "running" : status} saving={false} base={base} live={live} workspace={detail.workspace} onJump={onShowTab} />
        )}
        {hasTab && <button type="button" className="iconbtn small" onClick={onPause} aria-label={resumeLabel} title={resumeLabel}><Icon name={paused ? "play" : "pause"} size={14} /></button>}
        {hasTab && !paused && <button type="button" className="iconbtn small" onClick={onTakeOver} aria-label={t("sidepanel.takeover")} title={t("sidepanel.takeover")}><Icon name="user" size={14} /></button>}
        {hasTab && <button type="button" className="iconbtn small" onClick={onShowTab} aria-label={t("sidepanel.showtab")} title={t("sidepanel.showtab")}><Icon name="eye" size={14} /></button>}
        {lineStops(status) && <button type="button" className="iconbtn small danger" onClick={onStop} aria-label={t("sidepanel.stop")} title={t("sidepanel.stop")}><Icon name="stop" size={14} /></button>}
      </div>
      {unconfirmed && (
        <div className="sp-live-warn" role="alert">
          <span className="truncate">{t("sidepanel.stop.unconfirmed")}</span>
          <button type="button" className="link" onClick={onStop}>{t("sidepanel.retry")}</button>
          <button type="button" className="link" onClick={onOpenApp}>{t("sidepanel.open_app")}</button>
        </div>
      )}
    </div>
  );
}

// ── the approval card ──────────────────────────────────────────────────────────────────────

/** A pending action of the agent's, answered here: it takes the focus, so Enter allows once,
 *  Ctrl+Enter allows the like for this conversation and Esc refuses, without the composer hearing. */
function ApprovalCard({ approval, similar, onDecide }: { approval: Approval; similar: string | null; onDecide: (how: "allow" | "similar" | "deny") => void }) {
  const card = useRef<HTMLDivElement>(null);
  const [acked, setAcked] = useState<string | null>(null);
  useEffect(() => card.current?.focus(), []);
  const choose = (how: "allow" | "similar" | "deny") => {
    if (acked) return;
    // A moment's acknowledgement before it goes, so a key pressed by habit is seen to have done something.
    setAcked(how);
    window.setTimeout(() => onDecide(how), 150);
  };
  return (
    <div ref={card} className="sp-card" role="alertdialog" tabIndex={0} aria-label={t("composer.approval.title", { tool: approval.tool || "tool" })}
      onKeyDown={(e) => {
        const how = cardKey(e);
        if (!how) return;
        e.preventDefault();
        e.stopPropagation();
        choose(how === "similar" && !similar ? "allow" : how);
      }}>
      <div className="sp-card-title">{t("composer.approval.title", { tool: approval.tool || "tool" })}</div>
      {approval.detail && <div className="sp-card-detail mono">{approval.detail}</div>}
      <div className="sp-card-actions">
        <button type="button" className={`btn small primary ${acked === "allow" ? "acked" : ""}`} onClick={() => choose("allow")}>{t("composer.approve")} <kbd>Enter</kbd></button>
        {similar && <button type="button" className={`btn small ${acked === "similar" ? "acked" : ""}`} onClick={() => choose("similar")}>{t("composer.approve.similar", { label: similar })} <kbd>Ctrl+Enter</kbd></button>}
        <button type="button" className={`btn small ${acked === "deny" ? "acked" : ""}`} onClick={() => choose("deny")}>{t("composer.deny")} <kbd>Esc</kbd></button>
      </div>
      <div className="sp-card-foot sub">{t("sidepanel.card.foot")}</div>
    </div>
  );
}

// ── a screenshot, whole or a dragged part ──────────────────────────────────────────────────

function ShotCrop({ shot, onCancel, onAttach }: { shot: PageInfo & { image: string }; onCancel: () => void; onAttach: (file: File) => void }) {
  const img = useRef<HTMLImageElement>(null);
  const [drag, setDrag] = useState<Rect | null>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const at = (e: React.PointerEvent) => {
    const box = img.current!.getBoundingClientRect();
    return { x: Math.min(Math.max(0, e.clientX - box.left), box.width), y: Math.min(Math.max(0, e.clientY - box.top), box.height) };
  };
  async function attachPart() {
    const el = img.current;
    if (!el || !drag || Math.abs(drag.w) < 4 || Math.abs(drag.h) < 4) return onAttach(dataFile(shot.image, "screenshot.png"));
    const box = el.getBoundingClientRect();
    const r = cropRect(drag, { w: box.width, h: box.height }, { w: el.naturalWidth, h: el.naturalHeight });
    const canvas = document.createElement("canvas");
    canvas.width = r.w;
    canvas.height = r.h;
    canvas.getContext("2d")?.drawImage(el, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h);
    onAttach(dataFile(canvas.toDataURL("image/png"), "screenshot.png"));
  }
  const box = drag ? { left: Math.min(drag.x, drag.x + drag.w), top: Math.min(drag.y, drag.y + drag.h), width: Math.abs(drag.w), height: Math.abs(drag.h) } : null;
  return (
    <div className="sp-shot" role="dialog" aria-label={t("sidepanel.shot.title")}
      onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}>
      <div className="sp-shot-hint sub">{t("sidepanel.shot.hint")}</div>
      <div className="sp-shot-frame"
        onPointerDown={(e) => { const p = at(e); start.current = p; setDrag({ x: p.x, y: p.y, w: 0, h: 0 }); (e.target as Element).setPointerCapture?.(e.pointerId); }}
        onPointerMove={(e) => { if (!start.current) return; const p = at(e); setDrag({ x: start.current.x, y: start.current.y, w: p.x - start.current.x, h: p.y - start.current.y }); }}
        onPointerUp={() => { start.current = null; }}>
        <img ref={img} src={shot.image} alt={t("sidepanel.shot.title")} draggable={false} />
        {box && <div className="sp-shot-box" style={box} />}
      </div>
      <div className="sp-card-actions">
        <button type="button" className="btn small" onClick={onCancel}>{t("sidepanel.cancel")}</button>
        <button type="button" className="btn small primary" onClick={() => void attachPart()}>{drag && Math.abs(drag.w) >= 4 ? t("sidepanel.shot.part") : t("sidepanel.shot.whole")}</button>
      </div>
    </div>
  );
}
