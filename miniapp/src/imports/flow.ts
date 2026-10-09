// The import's state, shared by the desktop's explorer and the phone's screens: which program, which
// folder, which session, how it comes over, and the job that brings it.
//
// Both views draw the same requests in a different shape, so the requests live here once. A folder's
// listing, a preview and a job are each guarded by a request counter: the operator clicks through
// sessions faster than the host answers, and a slow answer for the session clicked before must never
// replace the one on screen.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { t } from "../i18n";
import {
  firstHarness, importBody, previewUrl, scanUrl, suggestedMode,
  type ForeignSession, type HarnessList, type ImportJob, type ImportMode, type ImportPreview, type ScanResult,
} from "./model";

export type Failure = { code: string; message: string; configured?: boolean; start?: string; install?: string };

/** What an entry point asks the explorer to open on: a program, a folder, a session. */
export type OpenRequest = { harness?: string; path?: string; session?: string };

export function failureOf(error: unknown): Failure {
  if (error instanceof ApiError) {
    const detail = error.data.detail;
    if (detail && typeof detail === "object" && typeof (detail as Failure).code === "string") return detail as Failure;
    if (error.status === 503) return { code: "host_down", message: errorText(error) };
    if (error.status === 501) return { code: "host_outdated", message: errorText(error) };
    return { code: "refused", message: errorText(error) };
  }
  return { code: "refused", message: errorText(error) };
}

/** A job as the views read it: an answer missing its counts or stage (a host mid-upgrade, a proxy's
 *  error page) must not take the window down with it. */
function steady(job: Partial<ImportJob>, id: string): ImportJob {
  return { ...job, job_id: job.job_id || id, state: job.state ?? "running", stage: job.stage ?? "read", counts: job.counts ?? {} } as ImportJob;
}

/** How long the search waits for the operator to stop typing before it asks the host. */
const SEARCH_WAIT_MS = 250;
/** How often a running import is asked how far it got. */
const JOB_POLL_MS = 400;

/** `onDone` is told the chat the import made and the project it joined, when it joined one. */
export function useImportFlow(initial: OpenRequest, onDone: (sessionId: string, into: string) => void) {
  const [list, setList] = useState<HarnessList | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [harness, setHarnessState] = useState(initial.harness ?? "");
  const [path, setPath] = useState(initial.path ?? "");
  const [history, setHistory] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [asked, setAsked] = useState("");
  const [deep, setDeep] = useState(false);
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [places, setPlaces] = useState<ScanResult | null>(null);
  const [selected, setSelected] = useState<ForeignSession | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [previewFailure, setPreviewFailure] = useState<Failure | null>(null);
  const [mode, setMode] = useState<ImportMode | null>(null);
  /** Empty until the operator picks one; the host's proposal (the source's own model) stands until then. */
  const [model, setModel] = useState("");
  const [makeProject, setMakeProject] = useState(false);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [jobFailure, setJobFailure] = useState<string | null>(null);
  /** The chat a refused import names: this session was imported already, as that chat. */
  const [already, setAlready] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const scanRequest = useRef(0);
  const previewRequest = useRef(0);
  const wanted = useRef(initial.session ?? "");
  const done = useRef(onDone);
  done.current = onDone;

  const loadHarnesses = useCallback(async () => {
    try {
      const result = await api.get<HarnessList>("/api/imports/harnesses");
      setList(result);
      setFailure(null);
      setHarnessState((current) => current && result.harnesses.some((h) => h.id === current) ? current : firstHarness(result.harnesses, initial.harness));
    } catch (error) {
      setFailure(failureOf(error));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { void loadHarnesses(); }, [loadHarnesses]);

  useEffect(() => {
    const timer = window.setTimeout(() => setAsked(query.trim()), query.trim() ? SEARCH_WAIT_MS : 0);
    return () => window.clearTimeout(timer);
  }, [query]);

  // The program's folders with sessions come with a scan of no folder, once per program: they are
  // the left column, and the first of them is where the explorer opens when nothing else was asked.
  useEffect(() => {
    if (!harness || failure) return;
    let stale = false;
    setPlaces(null);
    api.get<ScanResult>(scanUrl(harness, "", "", false)).then((result) => {
      if (stale) return;
      setPlaces(result);
      setPath((current) => current || result.folders[0]?.path || result.home || "");
    }).catch((error) => { if (!stale) setFailure(failureOf(error)); });
    return () => { stale = true; };
  }, [harness, failure]);

  const rescan = useCallback(async () => {
    if (!harness || (!path && !asked)) return;
    const mine = ++scanRequest.current;
    setScanning(true);
    try {
      // A search spans every folder of the program, so it is asked of no folder in particular.
      const result = await api.get<ScanResult>(scanUrl(harness, asked ? "" : path, asked, deep));
      if (mine !== scanRequest.current) return;
      setScan(result);
      setFailure(null);
      const want = wanted.current;
      if (want) {
        const found = result.here.find((s) => s.id === want);
        if (found) { setSelected(found); wanted.current = ""; }
      }
    } catch (error) {
      if (mine === scanRequest.current) setFailure(failureOf(error));
    } finally {
      if (mine === scanRequest.current) setScanning(false);
    }
  }, [harness, path, asked, deep]);

  useEffect(() => { void rescan(); }, [rescan]);

  useEffect(() => {
    if (!selected) { setPreview(null); setPreviewFailure(null); return; }
    const mine = ++previewRequest.current;
    setPreviewFailure(null);
    api.get<ImportPreview>(previewUrl(selected.harness, selected.id)).then((result) => {
      if (mine !== previewRequest.current) return;
      setPreview(result);
    }).catch((error) => { if (mine === previewRequest.current) { setPreview(null); setPreviewFailure(failureOf(error)); } });
  }, [selected]);

  // A job is asked how far it got until it says it is over; the chat it made then opens.
  useEffect(() => {
    if (!job || job.state !== "running") return;
    const timer = window.setTimeout(async () => {
      try {
        const next = steady(await api.get<ImportJob>(`/api/imports/${encodeURIComponent(job.job_id)}`), job.job_id);
        setJob(next);
        if (next.state === "done" && next.session_id) {
          invalidate("/api/sessions");
          invalidate("/api/projects");
          done.current(next.session_id, preview?.destination.project && preview.destination.kind === "project" ? preview.destination.project.name : "");
        }
        if (next.state === "failed") setJobFailure(next.error?.message || "");
      } catch (error) {
        setJobFailure(errorText(error));
      }
    }, JOB_POLL_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job]);

  function setHarness(id: string) {
    if (id === harness) return;
    setHarnessState(id);
    setPath("");
    setHistory([]);
    setScan(null);
    setSelected(null);
    setQuery("");
  }

  function go(next: string) {
    if (!next || next === path) return;
    if (path) setHistory((items) => [...items.slice(-30), path]);
    setQuery("");
    setPath(next);
  }

  function back() {
    const previous = history[history.length - 1];
    if (previous === undefined) return;
    setHistory((items) => items.slice(0, -1));
    setQuery("");
    setPath(previous);
  }

  function select(session: ForeignSession | null) {
    if (session?.id === selected?.id && session?.harness === selected?.harness) return;
    setSelected(session);
    setPreview(null);
    setMode(null);
    setModel("");
    setMakeProject(false);
    setJob(null);
    setJobFailure(null);
    setAlready(null);
  }

  async function start(again = false) {
    if (!preview || starting) return;
    setStarting(true);
    setJobFailure(null);
    try {
      const chosen = model || preview.model.preset || "";
      const body = importBody(preview, mode ?? suggestedMode(preview, model), chosen, makeProject, again);
      const answer = await api.post<{ job_id?: string; job?: ImportJob }>("/api/imports", body);
      const id = answer.job?.job_id ?? answer.job_id;
      if (!id) throw new Error(t("imp.failed"));
      setJob(steady(answer.job ?? { job_id: id, state: "running", stage: "read", counts: {} }, id));
    } catch (error) {
      const failure = failureOf(error) as Failure & { session_id?: string };
      // Imported already: the window offers the chat it became, or a second import as a new chat.
      if (failure.code === "already_imported" && failure.session_id) setAlready(failure.session_id);
      setJobFailure(failure.message || errorText(error));
    } finally {
      setStarting(false);
    }
  }

  return {
    list, failure, harness, setHarness, path, go, back, canBack: history.length > 0, query, setQuery, asked, deep, setDeep,
    scan, scanning, places, selected, select, preview, previewFailure, mode: mode ?? (preview ? suggestedMode(preview, model) : "full"), setMode,
    // The model the operator picked, else the one the host proposed: the source's own when it has it.
    model: model || preview?.model.preset || "", modelPicked: !!model, setModel, makeProject, setMakeProject, job, jobFailure, already, starting, start,
    retry: () => { setFailure(null); void loadHarnesses(); },
    rescan,
  };
}

export type ImportFlow = ReturnType<typeof useImportFlow>;
