// Settings → Components: the optional pieces of this installation, and the button that adds one.
//
// The owner opened Settings → Voice on a portable build and found two sentences about the browser's
// own synthesiser where the model pickers should have been. Nothing on that page said a download
// would put them there, because nothing anywhere did. This is that page: one card per component,
// with what it unlocks, what it costs, and either a button or the command that does it instead.
//
// Three things the cards are careful about, because each of them was a way to mislead:
//
//  * A button appears only where this process can really do the work. Everywhere else the card
//    carries the command or the image tag, which is a thing the operator can act on; a button that
//    answers 501 is not.
//  * A finished install that needs a restart says so, on the card and once at the top, with the
//    restart offered where the launcher can give it. Node and the browser are found through the
//    environment the agent was started with, so "installed" alone would be a half-truth.
//  * Progress is a live stream, not a spinner. A three-hundred-megabyte browser looks like one.

import { useCallback, useEffect, useState } from "react";
import { SearchComponent } from "../searchcomponent";
import { api } from "../api";
import { Icon } from "../icons";
import { modelSize as size } from "../format";
import { plural, t } from "../i18n";
import { Row } from "../settingsrow";
import { pathFor } from "../router";
import { go } from "../ui/index";
import { errorText, haptic } from "../ui";
import type { ComponentEntry, ComponentsView } from "../componentsview";
import {
  applyFrame,
  cancelComponent,
  fetchComponents,
  installComponent,
  installFrame,
  mergeComponents,
  restartAgent,
  restartPending,
  settled,
} from "../componentsview";

const label = (id: string) => t(`comp.name.${id}`);

/** The state word, as a chip. Colour carries the same thing the word does, never instead of it. */
function StateChip({ entry }: { entry: ComponentEntry }) {
  return <span className={`comp-chip comp-${entry.state}`}>{t(`comp.state.${entry.state}`)}</span>;
}

/** What is happening right now, under the card: the line the installer last published. */
function Progress({ entry }: { entry: ComponentEntry }) {
  const p = entry.progress;
  if (!p) return null;
  if (p.state === "failed") return <div className="sub attn comp-line">{t("comp.failed", { reason: p.error })}</div>;
  if (p.state === "queued" || p.state === "running") {
    // The step is whatever uv or the launcher last wrote, which is its output and stays as it was
    // written; the one step that is the app's own sentence comes as a key instead.
    const step = p.step_key ? t(p.step_key) : p.step || "…";
    return (
      <div className="comp-running">
        <span className="comp-bar">
          <span className="comp-bar-fill" />
        </span>
        <span className="sub faint comp-step">{t("comp.step", { step })}</span>
      </div>
    );
  }
  return null;
}

function Card({
  entry,
  view,
  busy,
  onInstall,
  onCancel,
}: {
  entry: ComponentEntry;
  view: ComponentsView;
  busy: string;
  onInstall: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const installing = entry.state === "installing";
  const failed = entry.progress?.state === "failed";
  const elsewhere = Boolean(busy) && busy !== entry.id;
  // The launcher is the one that fetches Node and the browser into the installation's own folder.
  // Without it running there is no button to offer, however native the installation is.
  const needsLauncher = entry.how === "launcher" && !view.launcher;
  const canInstall = entry.installable && !needsLauncher;
  return (
    <div className={`comp-card ${entry.state === "installed" ? "comp-card-on" : ""}`}>
      <div className="comp-head">
        <b>{label(entry.id)}</b>
        <StateChip entry={entry} />
      </div>
      <div className="sub comp-what">{t(`comp.what.${entry.id}`)}</div>

      {entry.total_count > 0 && (
        <div className="kv">
          <span>{t("comp.models.count", { n: String(entry.installed_count), total: String(entry.total_count) })}</span>
          <b>{entry.disk_bytes ? size(entry.disk_bytes) : "—"}</b>
        </div>
      )}

      <div className="comp-enables">
        <span className="sub faint">{t("comp.unlocks")}</span>
        <span className="comp-tags">
          {entry.enables.map((key) => (
            <span key={key} className="comp-tag">
              {t(`comp.enables.${key}`)}
            </span>
          ))}
        </span>
      </div>

      {entry.skills.length > 0 && <div className="sub faint comp-line">{plural("comp.skills", entry.skills.length)}: {entry.skills.join(", ")}</div>}

      <Progress entry={entry} />

      <div className="btnrow comp-actions">
        {entry.how === "models" && (
          <a className="btn small" href={pathFor("settings", "voice")} onClick={(e) => go(e, pathFor("settings", "voice"))}>
            {t("comp.models.open")}
          </a>
        )}
        {canInstall && !installing && entry.state !== "installed" && (
          <button className="btn small primary" disabled={elsewhere} onClick={() => onInstall(entry.id)}>
            {failed
              ? t("comp.retry")
              : entry.download_bytes
                ? t("comp.install.size", { size: size(entry.download_bytes) })
                : t("comp.install")}
          </button>
        )}
        {installing && (
          <button className="btn small" onClick={() => onCancel(entry.id)}>
            {t("comp.cancel")}
          </button>
        )}
      </div>

      {needsLauncher && <div className="sub attn comp-line">{t("comp.nolauncher")}</div>}
      {entry.state !== "installed" && !canInstall && entry.fix && (
        <div className="comp-byhand">
          <div className="sub faint">{t("comp.byhand")}</div>
          <code className="mono comp-fix">{entry.fix}</code>
        </div>
      )}
      {/* What the server found, in the reader's language and kept small: the card above it is the
          answer, this is the evidence. It is hidden while an install is going, because the fact it
          reports is the one the install is in the middle of changing and a live bar with "not
          installed" under it says two things at once. The two model cards are the other exception —
          their sentence is the count, and the count is already above. */}
      {!installing && entry.total_count === 0 && (entry.detail_key || entry.detail) && (
        <div className="sub faint comp-detail">{entry.detail_key ? t(entry.detail_key, entry.detail_args) : entry.detail}</div>
      )}
    </div>
  );
}

export function ComponentsTab({ toast }: { toast: (t: string) => void }) {
  const [view, setView] = useState<ComponentsView | null>(null);
  const [problem, setProblem] = useState("");
  const [restarting, setRestarting] = useState(false);

  const load = useCallback(async () => {
    try {
      const answer = await fetchComponents();
      setView((current) => mergeComponents(current, answer));
    } catch (e) {
      setProblem(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // The install stream. A frame moves one card; a finished one reloads the list, which is where
  // "installed" and the disk total come from.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/components/stream", { headers: api.authHeaders(), signal: controller.signal });
        if (!response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() ?? "";
          for (const chunk of chunks) {
            const data = /^data: (.*)$/m.exec(chunk)?.[1];
            if (!data) continue;
            try {
              const frame = installFrame(JSON.parse(data));
              if (!frame) continue;
              setView((v) => (v ? applyFrame(v, frame) : v));
              if (frame.state === "failed" && frame.error) setProblem(frame.error);
              if (settled(frame)) {
                if (frame.state === "installed") toast(t("comp.toast.installed", { name: label(frame.id) }));
                void load();
              }
            } catch {
              /* one malformed frame must not end the stream */
            }
          }
        }
      } catch {
        /* the page navigated away, or the stream dropped; the list still reloads on every action */
      }
    })();
    return () => controller.abort();
  }, [load, toast]);

  async function install(id: string) {
    setProblem("");
    try {
      await installComponent(id);
      haptic("medium");
      toast(t("comp.toast.started", { name: label(id) }));
    } catch (e) {
      setProblem(errorText(e));
    }
    void load();
  }

  async function cancel(id: string) {
    try {
      await cancelComponent(id);
    } catch (e) {
      setProblem(errorText(e));
    }
    void load();
  }

  async function restart() {
    setRestarting(true);
    try {
      await restartAgent();
      toast(t("comp.restart.asked"));
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setRestarting(false);
    }
  }

  if (!view) {
    return (
      <div className="card">
        <div className="sub">{problem || t("comp.loading")}</div>
      </div>
    );
  }

  const busy = view.busy || view.components.find((c) => c.state === "installing")?.id || "";
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>
        {t("comp.title")}
      </div>
      <div className="sub">{t("comp.intro")}</div>
      {/* How the installation runs and what the downloads take, as a row with the way to the other
          half of "what agents can use" as its action. */}
      <Row className="comp-mode" title={t(`comp.mode.${view.mode === "native" ? "native" : "docker"}`)} desc={view.disk_bytes > 0 ? t("comp.disk", { size: size(view.disk_bytes) }) : undefined}>
        <a className="btn small" href={pathFor("settings", "dependencies")} onClick={(event) => go(event, pathFor("settings", "dependencies"))}>{t("settings.sec.dependencies")}</a>
      </Row>

      {restartPending(view) && (
        <div className="comp-restart">
          <b>{t("comp.restart.title")}</b>
          <div className="sub">{t("comp.restart.why")}</div>
          <div className="btnrow" style={{ marginTop: 8 }}>
            <button className="btn small primary" disabled={restarting} onClick={restart}>
              {t("comp.restart.do")}
            </button>
          </div>
        </div>
      )}

      {problem && <div className="sub attn comp-line">{problem}</div>}

      <div className="comp-grid">
        <SearchComponent />
        {view.components.map((entry) => (
          <Card key={entry.id} entry={entry} view={view} busy={busy} onInstall={install} onCancel={cancel} />
        ))}
      </div>
    </div>
  );
}

/** The one-component version of the page, for the place where its absence is actually noticed.
 *
 * The owner's report was about Settings → Voice, not about a components page nobody had opened: the
 * pickers were missing and the page said nothing about a download. So the same install lives there
 * too, as one button with the size on it, and links to the full page for everything else. It renders
 * nothing at all when the runtime is present — a notice about a solved problem is noise.
 */
export function SpeechRuntimeNotice({ toast, onInstalled }: { toast: (t: string) => void; onInstalled?: () => void }) {
  const [entry, setEntry] = useState<ComponentEntry | null>(null);
  const [problem, setProblem] = useState("");
  const [asked, setAsked] = useState(false);

  const load = useCallback(async () => {
    try {
      const answer = await fetchComponents();
      setEntry(mergeComponents(null, answer).components.find((c) => c.id === "speech") ?? null);
    } catch {
      /* the card is an offer, not a gate: a page that cannot reach this endpoint still works */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Only while an install of our own is in flight: the page is not the components page and has no
  // business holding a stream open for the whole visit.
  useEffect(() => {
    if (!asked) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [asked, load]);

  // A finished install is over whichever way it finished. The card unmounts when it succeeds, which
  // stopped the poll by accident; one that failed left it running for as long as the page was open.
  useEffect(() => {
    if (asked && entry?.progress && settled(entry.progress)) setAsked(false);
  }, [asked, entry]);

  if (!entry || entry.state === "installed") return null;
  const installing = entry.state === "installing";

  async function install() {
    setProblem("");
    setAsked(true);
    try {
      await installComponent("speech");
      toast(t("voice.needs.installing"));
      haptic("medium");
    } catch (e) {
      setProblem(errorText(e));
    }
    await load();
    onInstalled?.();
  }

  return (
    <div className="voice-needs">
      <div className="sub">{t("voice.needs.speech")}</div>
      {problem && <div className="sub attn comp-line">{problem}</div>}
      {installing && <Progress entry={entry} />}
      <div className="btnrow">
        {entry.installable && (
          <button className="btn small primary" disabled={installing} onClick={install}>
            {installing ? t("voice.needs.installing") : t("voice.needs.install", { size: size(entry.download_bytes) })}
          </button>
        )}
        <a className="btn small" href={pathFor("settings", "components")} onClick={(e) => go(e, pathFor("settings", "components"))}>
          {t("voice.needs.elsewhere")}
        </a>
      </div>
    </div>
  );
}
