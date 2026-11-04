// The host's notes routes, typed. Every write answers with the note as it now is, which the panel puts
// straight into its listing so the change shows before the next read.

import { api, ApiError } from "../api";
import type { Note, NoteFile, NoteScope } from "./model";

export const notesApi = {
  create: (sessionId: string, body: { scope: NoteScope; title?: string; body?: string; color?: string; pinned?: boolean }) => api.post<Note>(`/api/sessions/${sessionId}/notes`, body),
  get: (id: string) => api.get<Note>(`/api/notes/${id}`),
  /** The words, with the version they were typed on; `force` keeps them over a newer version. */
  save: (id: string, body: { title?: string; body?: string; version: number; force?: boolean }) => api.patch<Note>(`/api/notes/${id}`, body),
  meta: (id: string, body: { pinned?: boolean; color?: string }) => api.patch<Note>(`/api/notes/${id}`, body),
  remove: (id: string) => api.delete<Note>(`/api/notes/${id}`),
  restore: (id: string) => api.post<Note>(`/api/notes/${id}/restore`),
  duplicate: (id: string, title: string) => api.post<Note>(`/api/notes/${id}/duplicate`, { title }),
  move: (id: string, sessionId: string, scope: NoteScope) => api.post<Note>(`/api/notes/${id}/move`, { session_id: sessionId, scope }),
  order: (sessionId: string, scope: NoteScope, ids: string[]) => api.post<{ notes: Note[] }>(`/api/sessions/${sessionId}/notes/order`, { scope, ids }),
  upload: (sessionId: string, scope: NoteScope, files: File[], onProgress?: (fraction: number) => void) => {
    const form = new FormData();
    form.append("scope", scope);
    for (const file of files) form.append("files", file, file.name);
    return api.upload<{ files: NoteFile[] }>(`/api/sessions/${sessionId}/notes/files`, form, onProgress);
  },
  references: (fileId: string) => api.get<{ notes: { id: string; title: string; scope: NoteScope }[] }>(`/api/notes/files/${fileId}/references`),
  removeFile: (fileId: string, force = false) => api.delete<NoteFile>(`/api/notes/files/${fileId}${force ? "?force=true" : ""}`),
};

/** The note a refused save carries: the version someone else saved since. */
export function conflictOf(e: unknown): Note | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = (e.data as { detail?: { note?: Note } }).detail;
  return detail?.note ?? null;
}

/** The notes that still show a file whose removal was refused. */
export function referencedBy(e: unknown): { id: string; title: string }[] | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const detail = (e.data as { detail?: { notes?: { id: string; title: string }[] } }).detail;
  return detail?.notes ?? null;
}

/** A file's bytes, once per file for the whole app: a picture shown by three notes and the files strip
 *  is fetched once. The URLs live as long as the page; note files never change under their id. */
const urls = new Map<string, Promise<string>>();

export function fileUrl(id: string): Promise<string> {
  let found = urls.get(id);
  if (!found) {
    found = fetch(`/api/notes/files/${id}/download`, { headers: api.authHeaders() }).then(async (res) => {
      if (!res.ok) throw new ApiError(res.status, `could not load the file (${res.status})`);
      return URL.createObjectURL(await res.blob());
    });
    // A failure is not remembered: the next look tries again.
    found.catch(() => urls.delete(id));
    urls.set(id, found);
  }
  return found;
}

/** A file just uploaded from this device: its bytes are here already, so it shows before any fetch. */
export function seedFileUrl(id: string, file: Blob): void {
  if (!urls.has(id)) urls.set(id, Promise.resolve(URL.createObjectURL(file)));
}

export async function fileBytes(id: string): Promise<Uint8Array> {
  const res = await fetch(await fileUrl(id));
  return new Uint8Array(await res.arrayBuffer());
}
