// The screen you land on: a greeting and the composer, so a chat begins by typing it.
// Naming it, choosing a folder and a loop stay in the new-agent sheet for when they matter.

import { type FocusEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Preset, Project, Settings } from "../api";
import { fieldHeight } from "../composer";
import { AttachmentCard } from "../composerbox";
import { ModelChoice, ModelSelect } from "../modelselect";
import { NewAgentSheet, SessionsScreen } from "./Sessions";
import { Icon } from "../icons";
import { navigate, pathFor, sessionPath, useRoute } from "../router";
import { invalidate, useQuery } from "../store";
import { useMedia } from "../shell";
import { enterSends, errorText } from "../ui";
import { t } from "../i18n";

export function StartScreen({ onOpen, toast, project = "", projects = [], onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; project?: string; projects?: Project[]; onProjects?: () => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const route = useRoute();
  const creating = route.query.get("new") === "1";
  const closeNew = () => navigate(pathFor("agents"), { replace: true });
  const finding = useFinding();
  return (
    <>
      <div className={`start ${phone && finding.on ? "finding" : ""}`}>
        <div className="start-hero">
          <h1 className="start-greeting">{t("start.greeting")}</h1>
          <StartComposer phone={phone} project={project} toast={toast} />
        </div>
      </div>
      {phone && (
        <div className="start-list" onFocus={finding.focus} onBlur={finding.blur}>
          <div className="start-list-head">
            <div className="section-title">{t("start.chats")}</div>
            {onProjects && <button type="button" className="iconbtn" onClick={onProjects} title={t("shell.projects")} aria-label={t("shell.projects")}><Icon name="skill" /></button>}
          </div>
          <SessionsScreen onOpen={onOpen} toast={toast} project={project} projects={projects} bare />
        </div>
      )}
      {creating && <NewAgentSheet onClose={closeNew} onCreated={onOpen} toast={toast} project={project} />}
    </>
  );
}

/** Whether the phone's search field has the reader: the greeting and its composer step aside then.
 *
 * On a phone the composer took the upper half of the screen above the search field, so the one
 * result a search found sat in the lower third behind an idle block. It hides while the field is
 * focused and stays hidden while a query is typed, so tapping a result does not bring it back first.
 * An empty field gives the composer back only once the finger is up: returning it on the blur that a
 * press causes would push the list down under that finger and the tap would land on another row. */
function useFinding() {
  const [on, setOn] = useState(false);
  const held = useRef(false);
  useEffect(() => {
    const down = () => { held.current = true; };
    const up = () => { held.current = false; };
    window.addEventListener("pointerdown", down, true);
    window.addEventListener("pointerup", up, true);
    window.addEventListener("pointercancel", up, true);
    return () => {
      window.removeEventListener("pointerdown", down, true);
      window.removeEventListener("pointerup", up, true);
      window.removeEventListener("pointercancel", up, true);
    };
  }, []);
  const isSearch = (el: EventTarget) => el instanceof HTMLInputElement && el.type === "search";
  const focus = (event: FocusEvent<HTMLDivElement>) => {
    if (isSearch(event.target)) setOn(true);
  };
  const blur = (event: FocusEvent<HTMLDivElement>) => {
    if (!isSearch(event.target) || (event.target as HTMLInputElement).value.trim()) return;
    // Back in a search field by the time the finger is up (the reader tapped it again): stay aside.
    const release = () => setTimeout(() => { if (!isSearch(document.activeElement ?? document.body)) setOn(false); }, 0);
    if (!held.current) release();
    else window.addEventListener("pointerup", release, { once: true });
  };
  return { on, focus, blur };
}

type Chosen = { preset: string } | { provider: string; model: string } | { model: string };

function StartComposer({ phone, project, toast }: { phone: boolean; project: string; toast: (t: string) => void }) {
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultId = settings.data?.model?.preset ?? "";
  const [draft, setDraft] = useState("");
  const [chosen, setChosen] = useState<Chosen | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const labelOf = (id: string) => {
    const item = presets[id] as Preset | undefined;
    return item ? item.label || `${item.provider}/${item.model}` : id;
  };
  const modelLabel = chosen
    ? "preset" in chosen ? labelOf(chosen.preset) : "provider" in chosen ? `${chosen.provider}/${chosen.model}` : chosen.model
    : defaultId ? labelOf(defaultId) : t("newagent.model.default");

  function choose(choice: ModelChoice) {
    if ("clear" in choice) setChosen(null);
    else if ("preset" in choice) setChosen({ preset: choice.preset });
    else if ("provider" in choice) setChosen({ provider: choice.provider, model: choice.model });
    else setChosen({ model: choice.model });
  }

  // A desktop lands here to type. A phone keeps the keyboard down until the field is touched:
  // opening it over the list of chats hides the thing the reader came to find.
  useEffect(() => {
    if (!phone) field.current?.focus();
  }, [phone]);
  const fit = () => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight);
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    el.style.height = `${fieldHeight(el.scrollHeight, line, pad, 1, phone ? 5 : undefined)}px`;
  };
  useLayoutEffect(fit, [draft, phone]);

  async function send() {
    const text = draft.trim();
    if (busy || (!text && files.length === 0)) return;
    setBusy(true);
    let created: { id: string } | null = null;
    let sent = false;
    try {
      // The message goes out after the session exists, so a model picked here is the one that
      // reads it. Sending both in the create call would start the run on the default model.
      created = await api.post<{ id: string }>("/api/sessions", {
        autotitle: true,
        title: text || files[0]?.name,
        preset: chosen && "preset" in chosen ? chosen.preset : undefined,
        project_id: project || undefined,
      });
      if (chosen && !("preset" in chosen)) {
        await api.post(`/api/sessions/${created.id}/model`, "provider" in chosen ? { provider: chosen.provider, model: chosen.model } : { model: chosen.model });
      }
      if (files.length) {
        const form = new FormData();
        if (text) form.append("text", text);
        for (const file of files) form.append("files", file);
        setProgress(0);
        await api.upload(`/api/sessions/${created.id}/upload`, form, setProgress);
      } else {
        await api.post(`/api/sessions/${created.id}/messages`, { text });
      }
      sent = true;
      setDraft("");
      setFiles([]);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
    if (created && sent) {
      invalidate("/api/sessions");
      navigate(sessionPath(created.id));
    } else if (created) {
      // A 150 MB video refused mid-upload used to leave the screen for the new, empty session: the
      // words and the file were gone and the list held a conversation with nothing in it. The draft
      // stays here to send again, and the empty session goes.
      void api.delete(`/api/sessions/${created.id}`).catch(() => undefined).finally(() => invalidate("/api/sessions"));
    }
  }

  // The picked files are read before the input is cleared: read inside the state update, which runs
  // later, they came from a list the clearing had already emptied, and nothing attached ever showed.
  const addFiles = (incoming: Iterable<File>) => {
    const picked = Array.from(incoming);
    if (picked.length) setFiles((held) => [...held, ...picked]);
  };
  const openFile = (file: File) => {
    const url = URL.createObjectURL(file);
    window.open(url, "_blank", "noopener");
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  return (
    <div className="start-composer composer" onDragOver={(event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); }} onDrop={(event) => { if (!event.dataTransfer?.files.length) return; event.preventDefault(); addFiles(event.dataTransfer.files); }}>
      <div className="composer-box">
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {files.length > 0 && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((file, i) => (
              <AttachmentCard key={`${file.name}-${file.size}-${file.lastModified}-${i}`} file={file} onOpen={() => openFile(file)} onRemove={() => setFiles((held) => held.filter((_, j) => j !== i))} />
            ))}
          </div>
        )}
        <textarea
          ref={field}
          value={draft}
          rows={1}
          placeholder={t("session.composer.idle")}
          aria-label={t("session.composer.idle")}
          onChange={(event) => setDraft(event.target.value)}
          onPaste={(event) => {
            const pasted = Array.from(event.clipboardData?.items ?? []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter((file): file is File => !!file);
            if (!pasted.length) return;
            event.preventDefault();
            addFiles(pasted);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && enterSends()) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-row">
          <input ref={fileInput} type="file" multiple hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
          <button type="button" className="iconbtn flat plus" onClick={() => fileInput.current?.click()} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
          <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet={phone} />
          <div className="composer-tools">
            <button type="button" className="roundbtn primary" onClick={() => void send()} disabled={busy || (!draft.trim() && files.length === 0)} aria-label={t("session.send")}><Icon name="up" /></button>
          </div>
        </div>
      </div>
    </div>
  );
}
