// The part of the extension API this extension uses, typed by hand. The published typings were
// passed over on purpose: they are a large, fast-moving package for the dozen calls made here, and
// the fake in `test/fake-chrome.ts` implements exactly this surface, so the two cannot drift apart
// without the type check saying so.

declare namespace chrome {
  export interface ChromeEvent<T extends (...args: any[]) => void> {
    addListener(callback: T): void;
    removeListener(callback: T): void;
  }

  export namespace runtime {
    interface Port {
      name: string;
      postMessage(message: unknown): void;
      disconnect(): void;
      onMessage: ChromeEvent<(message: any, port: Port) => void>;
      onDisconnect: ChromeEvent<(port: Port) => void>;
    }
    interface MessageSender {
      tab?: tabs.Tab;
      frameId?: number;
      id?: string;
      url?: string;
    }
    interface PlatformInfo {
      os: string;
      arch: string;
    }
    const id: string;
    let lastError: { message?: string } | undefined;
    function connectNative(application: string): Port;
    function sendMessage(message: unknown): Promise<any>;
    function getManifest(): { version: string; [key: string]: unknown };
    function getPlatformInfo(): Promise<PlatformInfo>;
    function getURL(path: string): string;
    const onMessage: ChromeEvent<
      (message: any, sender: MessageSender, sendResponse: (response?: unknown) => void) => boolean | void
    >;
    const onStartup: ChromeEvent<() => void>;
    const onInstalled: ChromeEvent<(details: { reason: string }) => void>;
  }

  export namespace storage {
    interface StorageArea {
      get(keys?: string | string[] | null): Promise<Record<string, any>>;
      set(items: Record<string, unknown>): Promise<void>;
      remove(keys: string | string[]): Promise<void>;
    }
    const local: StorageArea;
    const session: StorageArea;
  }

  export namespace tabs {
    interface Tab {
      id?: number;
      windowId: number;
      groupId: number;
      index: number;
      active: boolean;
      url?: string;
      pendingUrl?: string;
      title?: string;
      status?: string;
      openerTabId?: number;
    }
    interface UpdateInfo {
      status?: string;
      url?: string;
      title?: string;
      groupId?: number;
    }
    interface GroupOptions {
      tabIds: number | number[];
      groupId?: number;
      createProperties?: { windowId?: number };
    }
    function create(properties: { windowId?: number; url?: string; active?: boolean; openerTabId?: number }): Promise<Tab>;
    function get(tabId: number): Promise<Tab>;
    function query(info: { active?: boolean; lastFocusedWindow?: boolean; windowId?: number; groupId?: number }): Promise<Tab[]>;
    function update(tabId: number, properties: { active?: boolean; url?: string }): Promise<Tab>;
    function remove(tabIds: number | number[]): Promise<void>;
    function group(options: GroupOptions): Promise<number>;
    function ungroup(tabIds: number | number[]): Promise<void>;
    function sendMessage(tabId: number, message: unknown): Promise<any>;
    const onCreated: ChromeEvent<(tab: Tab) => void>;
    const onRemoved: ChromeEvent<(tabId: number, info: { windowId: number; isWindowClosing: boolean }) => void>;
    const onUpdated: ChromeEvent<(tabId: number, info: UpdateInfo, tab: Tab) => void>;
  }

  export namespace tabGroups {
    type Color = "grey" | "blue" | "red" | "yellow" | "green" | "pink" | "purple" | "cyan" | "orange";
    interface TabGroup {
      id: number;
      windowId: number;
      title?: string;
      color: Color;
      collapsed: boolean;
    }
    function get(groupId: number): Promise<TabGroup>;
    function update(groupId: number, properties: { title?: string; color?: Color; collapsed?: boolean }): Promise<TabGroup>;
    const onRemoved: ChromeEvent<(group: TabGroup) => void>;
  }

  export namespace windows {
    interface Window {
      id?: number;
      focused: boolean;
      left?: number;
      top?: number;
      width?: number;
      height?: number;
      state?: string;
      type?: string;
      tabs?: tabs.Tab[];
    }
    interface CreateData {
      url?: string;
      focused?: boolean;
      type?: "normal" | "popup";
      state?: "normal" | "minimized" | "maximized";
      width?: number;
      height?: number;
      left?: number;
      top?: number;
    }
    interface UpdateInfo {
      left?: number;
      top?: number;
      width?: number;
      height?: number;
      state?: "normal" | "minimized" | "maximized" | "fullscreen";
      focused?: boolean;
    }
    function create(data: CreateData): Promise<Window>;
    function get(windowId: number): Promise<Window>;
    function getLastFocused(options?: { windowTypes?: string[] }): Promise<Window>;
    function update(windowId: number, info: UpdateInfo): Promise<Window>;
    const onRemoved: ChromeEvent<(windowId: number) => void>;
  }

  namespace _debugger {
    interface Debuggee {
      tabId?: number;
    }
    // Chrome 125 added `sessionId`, which addresses a flat child session (an iframe of another site,
    // a worker) through the same debuggee. It is why `minimum_chrome_version` is 125.
    interface DebuggerSession {
      tabId?: number;
      sessionId?: string;
    }
    function attach(target: Debuggee, requiredVersion: string): Promise<void>;
    function detach(target: Debuggee): Promise<void>;
    function sendCommand(target: DebuggerSession, method: string, params?: object): Promise<any>;
    const onEvent: ChromeEvent<(source: DebuggerSession, method: string, params?: object) => void>;
    const onDetach: ChromeEvent<(source: Debuggee, reason: string) => void>;
  }

  export { _debugger as debugger };

  export namespace action {
    function setBadgeText(details: { text: string; tabId?: number }): Promise<void>;
    function setBadgeBackgroundColor(details: { color: string; tabId?: number }): Promise<void>;
    function setTitle(details: { title: string; tabId?: number }): Promise<void>;
  }

  export namespace contextMenus {
    interface CreateProperties {
      id: string;
      title: string;
      contexts?: string[];
      parentId?: string;
      enabled?: boolean;
    }
    interface OnClickData {
      menuItemId: string | number;
    }
    function create(properties: CreateProperties): void;
    function removeAll(): Promise<void>;
    const onClicked: ChromeEvent<(info: OnClickData, tab?: tabs.Tab) => void>;
  }

  export namespace scripting {
    function executeScript(injection: {
      target: { tabId: number; allFrames?: boolean };
      files: string[];
      injectImmediately?: boolean;
    }): Promise<unknown[]>;
  }

  export namespace cookies {
    interface Cookie {
      name: string;
      value: string;
      domain: string;
      path: string;
      secure: boolean;
      httpOnly: boolean;
      sameSite: string;
      session: boolean;
      expirationDate?: number;
      hostOnly: boolean;
    }
    function getAll(details: { url: string }): Promise<Cookie[]>;
  }

  export namespace downloads {
    interface DownloadItem {
      id: number;
      url: string;
      finalUrl?: string;
      filename: string;
      mime: string;
      state: string;
      startTime: string;
      endTime?: string;
      bytesReceived: number;
      totalBytes: number;
      error?: string;
      exists: boolean;
    }
    function search(query: { id?: number }): Promise<DownloadItem[]>;
  }

  export namespace declarativeNetRequest {
    interface Rule {
      id: number;
      priority?: number;
      action: { type: "block" | "allow" };
      condition: { regexFilter?: string; tabIds?: number[]; requestDomains?: string[]; resourceTypes?: string[] };
    }
    function updateSessionRules(options: { removeRuleIds?: number[]; addRules?: Rule[] }): Promise<void>;
  }

  export namespace alarms {
    function create(name: string, info: { periodInMinutes?: number; delayInMinutes?: number }): Promise<void>;
    const onAlarm: ChromeEvent<(alarm: { name: string }) => void>;
  }

  export namespace i18n {
    function getUILanguage(): string;
  }
}
