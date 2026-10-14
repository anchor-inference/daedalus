// How files reach anything in the app that takes them — the composer, an answer in the Questions
// tab: what the clipboard holds, and what a pasted screenshot is called. Shared so every field that
// takes a paste names screenshots the same way and finds the same files on the clipboard.

/** The files as they are kept: a pasted screenshot arrives as "image.png" every time, so each one
 *  gets a name of its own (`screenshot-20261014-101500.png`). Files with real names pass unchanged. */
export function nameIncoming(incoming: Iterable<File>, now: Date = new Date()): File[] {
  return Array.from(incoming).map((f) => {
    if (!/^(image|blob|file)(\.[a-z0-9]+)?$/i.test(f.name)) return f;
    const ext = f.name.includes(".") ? f.name.slice(f.name.lastIndexOf(".")) : f.type.startsWith("image/") ? `.${f.type.slice(6).replace("jpeg", "jpg")}` : "";
    const stamp = now.toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
    return new File([f], `${f.type.startsWith("image/") ? "screenshot" : "pasted"}-${stamp}${ext}`, { type: f.type, lastModified: f.lastModified });
  });
}

/** The files a paste carries, or none for a paste of text. */
export function pastedFiles(data: Pick<DataTransfer, "items"> | null | undefined): File[] {
  return Array.from(data?.items ?? []).filter((it) => it.kind === "file").map((it) => it.getAsFile()).filter((f): f is File => !!f);
}

/** The images on the system clipboard, read on a button press (the + menu's Paste) where there is no
 *  paste event to take them from. Empty where the browser cannot read the clipboard. */
export async function clipboardImages(): Promise<File[]> {
  const items = navigator.clipboard?.read ? await navigator.clipboard.read() : [];
  const got: File[] = [];
  for (const item of items) {
    const type = item.types.find((x) => x.startsWith("image/"));
    if (!type) continue;
    const blob = await item.getType(type);
    got.push(new File([blob], `image.${type.slice(6).replace("jpeg", "jpg")}`, { type }));
  }
  return got;
}
