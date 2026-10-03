// Browser storage keeps selected attachments with their session draft. A File cannot be rebuilt
// from its name after a reload; if storage fails, the composer must say so before the user leaves.

const DATABASE = "daedalus-composer-drafts";
const STORE = "attachments";
const VERSION = 1;

type Stored = { sessionId: string; files: File[] };

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) return reject(new Error("IndexedDB unavailable"));
    const request = indexedDB.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE, { keyPath: "sessionId" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Draft storage unavailable"));
  });
}

export async function loadDraftFiles(sessionId: string): Promise<File[]> {
  await pending.get(sessionId)?.catch(() => undefined);
  const db = await database();
  try {
    return await new Promise<File[]>((resolve, reject) => {
      const transaction = db.transaction(STORE, "readonly");
      const request = transaction.objectStore(STORE).get(sessionId);
      request.onsuccess = () => resolve((request.result as Stored | undefined)?.files ?? []);
      request.onerror = () => reject(request.error ?? new Error("Draft read failed"));
    });
  } finally {
    db.close();
  }
}

const pending = new Map<string, Promise<void>>();

export function saveDraftFiles(sessionId: string, files: File[]): Promise<void> {
  const previous = pending.get(sessionId) ?? Promise.resolve();
  const write = previous.catch(() => undefined).then(async () => {
    const db = await database();
    try {
      await new Promise<void>((resolve, reject) => {
        const transaction = db.transaction(STORE, "readwrite");
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error ?? new Error("Draft write failed"));
        transaction.onabort = () => reject(transaction.error ?? new Error("Draft write aborted"));
        if (files.length) transaction.objectStore(STORE).put({ sessionId, files });
        else transaction.objectStore(STORE).delete(sessionId);
      });
    } finally {
      db.close();
    }
  });
  pending.set(sessionId, write);
  void write.finally(() => { if (pending.get(sessionId) === write) pending.delete(sessionId); }).catch(() => undefined);
  return write;
}
