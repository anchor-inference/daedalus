// What the side panel attaches from the operator's page: a picture of what is on screen, the page's
// text, the words they selected. Each acts on the active tab of the panel's own window, only when the
// operator asked a moment before (the bridge checks that), and returns what the page's own address
// and title are, so the conversation says where the attachment came from.

const TEXT_LIMIT = 100_000;
const SELECTION_LIMIT = 20_000;

export type PageInfo = { tab: number; url: string; title: string };

/** Chrome's own pages and other extensions' cannot be read or photographed by an extension. */
export function readable(url: string | undefined): boolean {
  return !!url && /^(https?|file):/i.test(url);
}

async function activeTab(windowId: number): Promise<chrome.tabs.Tab & { id: number }> {
  const [tab] = await chrome.tabs.query({ active: true, windowId });
  if (!tab || tab.id === undefined) throw new Error("no_tab");
  if (!readable(tab.url)) throw new Error("unreadable");
  return tab as chrome.tabs.Tab & { id: number };
}

function info(tab: chrome.tabs.Tab & { id: number }): PageInfo {
  return { tab: tab.id, url: tab.url ?? "", title: tab.title ?? "" };
}

/** The visible part of the active tab, as a PNG data URL. A region is cut from it in the frame,
 *  where the operator drags it; the page itself is never drawn over. */
export async function captureVisible(windowId: number): Promise<PageInfo & { image: string }> {
  const tab = await activeTab(windowId);
  const image = await chrome.tabs.captureVisibleTab(windowId, { format: "png" });
  return { ...info(tab), image };
}

export async function pageText(windowId: number): Promise<PageInfo & { text: string }> {
  const tab = await activeTab(windowId);
  const [found] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => (document.body ? document.body.innerText : ""),
  });
  return { ...info(tab), text: String(found?.result ?? "").slice(0, TEXT_LIMIT) };
}

export async function selection(windowId: number): Promise<PageInfo & { text: string }> {
  const tab = await activeTab(windowId);
  const [found] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => String(globalThis.getSelection?.() ?? ""),
  });
  return { ...info(tab), text: String(found?.result ?? "").slice(0, SELECTION_LIMIT) };
}
