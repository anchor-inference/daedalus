// @vitest-environment jsdom
// The files an operator's message carried, drawn above the bubble: the pictures as thumbnails that
// open in the viewer, every other file as a card with its kind, and the list the host wrote for the
// model never printed in the bubble — for the history written before the host recorded the files too.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MessageAttachment } from "./api";
import { MessageAttachments } from "./attachcards";
import { attachmentKind, attachmentSource, splitAttachments } from "./attachments";
import { setLang } from "./i18n";
import { withoutAttachedList } from "./keptfiles";
import { conversationMedia } from "./mediaroute";
import type { PreviewSource } from "./preview";
import type { Turn } from "./turns";

const GUID = "{2C33CD0A-830B-4C8A-B4EE-59B46E35D59E}.png";
const ITEMS: MessageAttachment[] = [
  { name: GUID, mime: "image/png", size: 182143, file: GUID },
  { name: "Отчёт за квартал.pdf", mime: "application/pdf", size: 5120, file: "Отчёт за квартал.pdf" },
  { name: "contract v2.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size: 2048, file: "contract v2.docx" },
  { name: "bundle.zip", mime: "application/zip", size: 99, kept: "0123456789ab" },
];

describe("what kind of file an attachment is", () => {
  it("reads the type first and the name second", () => {
    expect(ITEMS.map(attachmentKind)).toEqual(["image", "pdf", "document", "archive"]);
    expect(attachmentKind({ name: "clipboard", mime: "image/jpeg" })).toBe("image");
    expect(attachmentKind({ name: "budget.xlsx" })).toBe("sheet");
    expect(attachmentKind({ name: "deck.pptx" })).toBe("slides");
    expect(attachmentKind({ name: "main.py", mime: "text/x-python" })).toBe("code");
    expect(attachmentKind({ name: "notes.txt", mime: "text/plain" })).toBe("text");
    expect(attachmentKind({ name: "voice.ogg", mime: "audio/ogg" })).toBe("audio");
    expect(attachmentKind({ name: "clip.mp4", mime: "video/mp4" })).toBe("video");
    expect(attachmentKind({ name: "blob.bin", mime: "application/octet-stream" })).toBe("file");
  });

  it("is served from the session's attachments by inbox name, or by handle when kept", () => {
    expect(attachmentSource("s1", ITEMS[0])).toEqual({ base: "/api/sessions/s1/attachments", path: GUID });
    expect(attachmentSource("s1", ITEMS[3])).toEqual({ base: "/api/files/0123456789ab", path: "bundle.zip" });
    const { images, files } = splitAttachments(ITEMS);
    expect(images.map((item) => item.name)).toEqual([GUID]);
    expect(files.map((item) => item.name)).toEqual(["Отчёт за квартал.pdf", "contract v2.docx", "bundle.zip"]);
  });
});

describe("the bubble's words", () => {
  it("drops the plain list, whatever the paths hold, and keeps the operator's words and what follows", () => {
    const text = [
      "check this out (the tester wrote it). Just check it.",
      "",
      "Attached files:",
      `- /home/someone/work/inbox/${GUID} (image/png, 182143 bytes)`,
      "- C:\\Users\\someone\\Daedalus\\projects\\p\\inbox\\photo (1).png (image/png, 12 bytes)",
      "- /data/workspaces/s1/inbox/Отчёт за квартал.pdf (application/pdf, 5120 bytes) — cookies for shop.test; BrowserCookies can load them into the browser",
    ].join("\n");
    expect(withoutAttachedList(text)).toBe("check this out (the tester wrote it). Just check it.");
    expect(withoutAttachedList(`${text}\n\n[reminder] water the plants`)).toBe("check this out (the tester wrote it). Just check it.\n\n[reminder] water the plants");
  });

  it("joins an item a transport broke in two, and leaves a header that lists nothing the host wrote", () => {
    expect(withoutAttachedList(`look\n\nAttached files:\n- /home/someone/inbox/${GUID}\n(image/png, 182143 bytes)`)).toBe("look");
    expect(withoutAttachedList("Attached files:\n- sample.txt")).toBe("Attached files:\n- sample.txt");
  });

  it("says nothing for an upload that had no words", () => {
    expect(withoutAttachedList(`File attached.\n\nAttached files:\n- /w/inbox/${GUID} (image/png, 5 bytes)`)).toBe("");
    expect(withoutAttachedList("Files attached.\n\nAttached files:\n- /w/inbox/a.pdf (application/pdf, 5 bytes)\n- /w/inbox/b.pdf (application/pdf, 6 bytes)")).toBe("");
    expect(withoutAttachedList("File attached.")).toBe("File attached.");
  });

  it("treats the last list as the host's", () => {
    const quoted = "He wrote:\nAttached files:\n- /x/inbox/old.png (image/png, 1 bytes)\n\nmine\n\nAttached files:\n- /w/inbox/new.png (image/png, 2 bytes)";
    expect(withoutAttachedList(quoted)).toBe("He wrote:\nAttached files:\n- /x/inbox/old.png (image/png, 1 bytes)\n\nmine");
  });
});

describe("the attachments above a message", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    setLang("en");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" }), { status: 200, headers: { "Content-Type": "image/png" } })));
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:thumb", revokeObjectURL: () => undefined }));
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
    setLang("en");
  });

  it("draws a thumbnail per picture and a card per file, each opening where the app shows it", async () => {
    const opened: PreviewSource[] = [];
    const saved = vi.fn();
    vi.stubGlobal("open", saved);
    await act(async () => root.render(<MessageAttachments sessionId="s1" items={ITEMS} onOpen={(src) => opened.push(src)} />));
    const thumbs = host.querySelectorAll<HTMLButtonElement>(".msg-thumbs .msg-thumb");
    expect(thumbs).toHaveLength(1);
    await vi.waitFor(() => expect(host.querySelector(".msg-thumb img")?.getAttribute("src")).toBe("blob:thumb"));
    const cards = [...host.querySelectorAll<HTMLButtonElement>(".msg-file")];
    expect(cards.map((card) => [card.dataset.kind, card.querySelector(".msg-file-name")?.textContent, card.querySelector(".msg-file-kind")?.textContent])).toEqual([
      ["pdf", "Отчёт за квартал.pdf", "PDF"],
      ["document", "contract v2.docx", "Document"],
      ["archive", "bundle.zip", "Archive"],
    ]);
    await act(async () => thumbs[0].click());
    await act(async () => cards[0].click());
    expect(opened).toEqual([
      { base: "/api/sessions/s1/attachments", path: GUID },
      { base: "/api/sessions/s1/attachments", path: "Отчёт за квартал.pdf" },
    ]);
    // A zip is nothing the app shows: it is downloaded, by handle.
    await act(async () => cards[2].click());
    expect(saved).toHaveBeenCalledTimes(1);
    expect(String(saved.mock.calls[0][0])).toMatch(/^\/api\/files\/0123456789ab\/download\?path=bundle\.zip/);
  });

  it("labels the kinds in Russian", async () => {
    setLang("ru");
    await act(async () => root.render(<MessageAttachments sessionId="s1" items={ITEMS.slice(1, 3)} onOpen={() => undefined} />));
    expect([...host.querySelectorAll(".msg-file-kind")].map((node) => node.textContent)).toEqual(["PDF", "Документ"]);
  });

  it("draws nothing for a message without files", async () => {
    await act(async () => root.render(<MessageAttachments sessionId="s1" items={[]} onOpen={() => undefined} />));
    expect(host.innerHTML).toBe("");
  });
});

describe("the viewer's list", () => {
  it("opens the conversation with the operator's own pictures", () => {
    const turn = { key: "t", user: { role: "user", text: "", thinking: "", tool_calls: [], tool_results: [], created_at: "", attachments: ITEMS }, activity: [] } as unknown as Turn;
    expect(conversationMedia([turn], "s1", "/work", [])).toEqual([{ base: "/api/sessions/s1/attachments", path: GUID }]);
  });
});
