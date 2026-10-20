// A small in-memory Chrome: windows, tabs, tab groups, debugger sessions, storage and the rest of the
// surface `src/chrome.d.ts` declares. It behaves the way the relay depends on Chrome behaving (a new
// window comes with one blank tab, grouping fires `tabs.onUpdated`, removing a group's last tab
// removes the group) and records what was done, so a test can assert on it.

type Listener = (...args: any[]) => unknown;

export class FakeEvent {
  listeners: Listener[] = [];
  addListener(listener: Listener) {
    this.listeners.push(listener);
  }
  removeListener(listener: Listener) {
    this.listeners = this.listeners.filter((l) => l !== listener);
  }
  emit(...args: unknown[]) {
    for (const listener of this.listeners) listener(...args);
  }
}

class Area {
  data: Record<string, unknown> = {};
  async get(keys?: string | string[] | null) {
    if (keys == null) return structuredClone(this.data);
    const out: Record<string, unknown> = {};
    for (const key of Array.isArray(keys) ? keys : [keys]) if (key in this.data) out[key] = structuredClone(this.data[key]);
    return out;
  }
  async set(items: Record<string, unknown>) {
    for (const [key, value] of Object.entries(items)) this.data[key] = structuredClone(value);
  }
  async remove(keys: string | string[]) {
    for (const key of Array.isArray(keys) ? keys : [keys]) delete this.data[key];
  }
}

export type FakeTab = { id: number; windowId: number; groupId: number; index: number; active: boolean; url: string; title: string; openerTabId?: number; status?: string };

export function installFakeChrome(options: { language?: string } = {}) {
  let nextTab = 100;
  let nextWindow = 10;
  let nextGroup = 500;
  const tabs = new Map<number, FakeTab>();
  const windows = new Map<number, { id: number; focused: boolean; left: number; top: number; width: number; height: number; state: string; type: string }>();
  const groups = new Map<number, { id: number; windowId: number; title?: string; color: string; collapsed: boolean }>();
  const attached = new Set<number>();
  const commands: { target: { tabId?: number; sessionId?: string }; method: string; params?: object }[] = [];
  const tabMessages: { tabId: number; message: any }[] = [];
  const injected: number[] = [];
  const badge = { text: "", color: "", title: "" };
  const menus: { id: string; title: string; parentId?: string; enabled?: boolean }[] = [];
  let dnrRules: any[] = [];
  const nativePorts: any[] = [];
  const runtimeMessages: any[] = [];
  const notifications: { id: string; options: any }[] = [];
  const sidePanel = { behavior: null as null | { openPanelOnActionClick: boolean }, opened: [] as any[] };
  const captures: number[] = [];
  const scripts: { tabId: number; func?: string }[] = [];
  let pageScript: (tabId: number, func: () => unknown) => unknown = () => "";
  let commandHandler: (method: string, params: any, target: any) => any = () => ({});
  let attachError: string | null = null;

  const events = {
    tabsCreated: new FakeEvent(),
    tabsRemoved: new FakeEvent(),
    tabsUpdated: new FakeEvent(),
    groupRemoved: new FakeEvent(),
    debuggerEvent: new FakeEvent(),
    debuggerDetach: new FakeEvent(),
    runtimeMessage: new FakeEvent(),
    menuClicked: new FakeEvent(),
    alarm: new FakeEvent(),
    windowRemoved: new FakeEvent(),
    tabsActivated: new FakeEvent(),
    runtimeConnect: new FakeEvent(),
    notificationClicked: new FakeEvent(),
  };

  function addWindow(focused = false) {
    const id = nextWindow++;
    windows.set(id, { id, focused, left: 0, top: 0, width: 1280, height: 900, state: "normal", type: "normal" });
    return id;
  }

  function addTab(windowId: number, url = "about:blank", extra: Partial<FakeTab> = {}): FakeTab {
    const id = nextTab++;
    const tab: FakeTab = { id, windowId, groupId: -1, index: tabs.size, active: false, url, title: "", ...extra };
    tabs.set(id, tab);
    return tab;
  }

  function dropEmptyGroups() {
    for (const group of [...groups.values()]) {
      if (![...tabs.values()].some((tab) => tab.groupId === group.id)) {
        groups.delete(group.id);
        events.groupRemoved.emit({ ...group });
      }
    }
  }

  function setGroup(tabId: number, groupId: number) {
    const tab = tabs.get(tabId);
    if (!tab) throw new Error(`No tab with id: ${tabId}.`);
    tab.groupId = groupId;
    events.tabsUpdated.emit(tabId, { groupId }, { ...tab });
    dropEmptyGroups();
  }

  const operatorWindow = addWindow(true);

  const chromeObject = {
    runtime: {
      id: "lkhhklicnjlplokpadpccndipomikoaj",
      lastError: undefined as { message?: string } | undefined,
      connectNative(name: string) {
        const port = {
          name,
          sent: [] as unknown[],
          disconnected: false,
          onMessage: new FakeEvent(),
          onDisconnect: new FakeEvent(),
          postMessage(message: unknown) {
            port.sent.push(structuredClone(message));
          },
          disconnect() {
            port.disconnected = true;
          },
        };
        nativePorts.push(port);
        return port;
      },
      async sendMessage(message: unknown) {
        runtimeMessages.push(message);
      },
      getManifest: () => ({ version: "0.1.0" }),
      getPlatformInfo: async () => ({ os: "linux", arch: "x86-64" }),
      getURL: (path: string) => `chrome-extension://lkhhklicnjlplokpadpccndipomikoaj/${path}`,
      connect(info?: { name?: string }) {
        const port = { name: info?.name ?? "", sent: [] as unknown[], onMessage: new FakeEvent(), onDisconnect: new FakeEvent(), postMessage(m: unknown) { port.sent.push(m); }, disconnect() {} };
        return port;
      },
      onConnect: events.runtimeConnect,
      onMessage: events.runtimeMessage,
      onStartup: new FakeEvent(),
      onInstalled: new FakeEvent(),
    },
    storage: { local: new Area(), session: new Area() },
    tabs: {
      async create(properties: { windowId?: number; url?: string; active?: boolean; openerTabId?: number }) {
        const windowId = properties.windowId ?? operatorWindow;
        const tab = addTab(windowId, properties.url ?? "about:blank", { active: properties.active ?? true, openerTabId: properties.openerTabId });
        events.tabsCreated.emit({ ...tab });
        return { ...tab };
      },
      async get(tabId: number) {
        const tab = tabs.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}.`);
        return { ...tab };
      },
      async query(info: { active?: boolean; windowId?: number } = {}) {
        return [...tabs.values()]
          .filter((tab) => (info.active === undefined || tab.active === info.active) && (info.windowId === undefined || tab.windowId === info.windowId))
          .map((tab) => ({ ...tab }));
      },
      async update(tabId: number, properties: { active?: boolean; url?: string }) {
        const tab = tabs.get(tabId);
        if (!tab) throw new Error(`No tab with id: ${tabId}.`);
        Object.assign(tab, properties);
        return { ...tab };
      },
      async remove(ids: number | number[]) {
        for (const id of Array.isArray(ids) ? ids : [ids]) {
          const tab = tabs.get(id);
          if (!tab) continue;
          tabs.delete(id);
          attached.delete(id);
          events.tabsRemoved.emit(id, { windowId: tab.windowId, isWindowClosing: false });
        }
        dropEmptyGroups();
      },
      async group(options: { tabIds: number | number[]; groupId?: number; createProperties?: { windowId?: number } }) {
        let groupId = options.groupId;
        const ids = Array.isArray(options.tabIds) ? options.tabIds : [options.tabIds];
        if (groupId === undefined) {
          groupId = nextGroup++;
          const windowId = options.createProperties?.windowId ?? tabs.get(ids[0])!.windowId;
          groups.set(groupId, { id: groupId, windowId, color: "grey", collapsed: false });
        } else if (!groups.has(groupId)) {
          throw new Error(`No group with id: ${groupId}.`);
        }
        const window = windows.get(tabs.get(ids[0])!.windowId);
        if (window?.type === "popup") throw new Error("Tabs can only be moved to and from normal windows.");
        for (const id of ids) setGroup(id, groupId);
        return groupId;
      },
      async ungroup(ids: number | number[]) {
        for (const id of Array.isArray(ids) ? ids : [ids]) setGroup(id, -1);
      },
      async sendMessage(tabId: number, message: unknown) {
        tabMessages.push({ tabId, message });
      },
      async captureVisibleTab(windowId: number) {
        captures.push(windowId);
        return "data:image/png;base64,AAAA";
      },
      onActivated: events.tabsActivated,
      onCreated: events.tabsCreated,
      onRemoved: events.tabsRemoved,
      onUpdated: events.tabsUpdated,
    },
    tabGroups: {
      async get(groupId: number) {
        const group = groups.get(groupId);
        if (!group) throw new Error(`No group with id: ${groupId}.`);
        return { ...group };
      },
      async update(groupId: number, properties: { title?: string; color?: string }) {
        const group = groups.get(groupId);
        if (!group) throw new Error(`No group with id: ${groupId}.`);
        Object.assign(group, properties);
        return { ...group };
      },
      onRemoved: events.groupRemoved,
    },
    windows: {
      async create(data: { focused?: boolean; url?: string; type?: string }) {
        const id = addWindow(data.focused ?? true);
        windows.get(id)!.type = data.type ?? "normal";
        const tab = addTab(id, data.url ?? "about:blank", { active: true });
        return { ...windows.get(id)!, tabs: [{ ...tab }] };
      },
      async get(windowId: number) {
        const window = windows.get(windowId);
        if (!window) throw new Error(`No window with id: ${windowId}.`);
        return { ...window };
      },
      async getLastFocused() {
        return { ...windows.get(operatorWindow)! };
      },
      async getCurrent() {
        return { ...windows.get(operatorWindow)! };
      },
      async update(windowId: number, info: Record<string, unknown>) {
        const window = windows.get(windowId);
        if (!window) throw new Error(`No window with id: ${windowId}.`);
        Object.assign(window, info);
        return { ...window };
      },
      onRemoved: events.windowRemoved,
    },
    debugger: {
      async attach(target: { tabId?: number }) {
        if (attachError) throw new Error(attachError);
        if (!tabs.has(target.tabId!)) throw new Error(`No tab with given id ${target.tabId}.`);
        if (attached.has(target.tabId!)) throw new Error(`Another debugger is already attached to the tab with id: ${target.tabId}.`);
        attached.add(target.tabId!);
      },
      async detach(target: { tabId?: number }) {
        if (!attached.delete(target.tabId!)) throw new Error("Debugger is not attached to the tab");
      },
      async sendCommand(target: { tabId?: number; sessionId?: string }, method: string, params?: object) {
        commands.push({ target: { ...target }, method, params });
        if (!attached.has(target.tabId!)) throw new Error("Debugger is not attached to the tab");
        return commandHandler(method, params, target);
      },
      onEvent: events.debuggerEvent,
      onDetach: events.debuggerDetach,
    },
    action: {
      async setBadgeText(details: { text: string }) {
        badge.text = details.text;
      },
      async setBadgeBackgroundColor(details: { color: string }) {
        badge.color = details.color;
      },
      async setTitle(details: { title: string }) {
        badge.title = details.title;
      },
    },
    contextMenus: {
      create(properties: { id: string; title: string; parentId?: string; enabled?: boolean }) {
        menus.push(properties);
      },
      async removeAll() {
        menus.length = 0;
      },
      onClicked: events.menuClicked,
    },
    scripting: {
      async executeScript(injection: { target: { tabId: number }; files?: string[]; func?: () => unknown }) {
        if (injection.func) {
          scripts.push({ tabId: injection.target.tabId, func: String(injection.func) });
          return [{ result: pageScript(injection.target.tabId, injection.func) }];
        }
        injected.push(injection.target.tabId);
        return [];
      },
    },
    sidePanel: {
      async setPanelBehavior(behavior: { openPanelOnActionClick: boolean }) {
        sidePanel.behavior = behavior;
      },
      async open(options: unknown) {
        sidePanel.opened.push(options);
      },
    },
    notifications: {
      async create(id: string, options: unknown) {
        notifications.push({ id, options });
        return id;
      },
      async clear(id: string) {
        const index = notifications.findIndex((n) => n.id === id);
        if (index >= 0) notifications.splice(index, 1);
        return index >= 0;
      },
      onClicked: events.notificationClicked,
    },
    cookies: {
      async getAll() {
        return [
          { name: "sid", value: "secret-value", domain: "example.org", path: "/", secure: true, httpOnly: true, sameSite: "lax", session: false, expirationDate: 1893456000, hostOnly: true },
        ];
      },
    },
    downloads: {
      items: [] as any[],
      async search(query: { id?: number; startedAfter?: string; orderBy?: string[]; limit?: number }) {
        let items = chromeObject.downloads.items.filter((item) =>
          (query.id === undefined || item.id === query.id) &&
          (query.startedAfter === undefined || Date.parse(item.startTime) > Date.parse(query.startedAfter)));
        if (query.orderBy?.[0] === "-startTime") items = items.sort((a, b) => Date.parse(b.startTime) - Date.parse(a.startTime));
        return items.slice(0, query.limit ?? items.length);
      },
    },
    declarativeNetRequest: {
      async updateSessionRules(options: { removeRuleIds?: number[]; addRules?: any[] }) {
        dnrRules = dnrRules.filter((rule) => !(options.removeRuleIds ?? []).includes(rule.id)).concat(options.addRules ?? []);
      },
    },
    alarms: {
      async create() {},
      onAlarm: events.alarm,
    },
    i18n: { getUILanguage: () => options.language ?? "en-US" },
  };

  (globalThis as any).chrome = chromeObject;

  return {
    chrome: chromeObject,
    events,
    tabs,
    windows,
    groups,
    attached,
    commands,
    tabMessages,
    injected,
    badge,
    menus,
    nativePorts,
    runtimeMessages,
    notifications,
    sidePanel,
    captures,
    scripts,
    operatorWindow,
    get dnrRules() {
      return dnrRules;
    },
    addTab,
    addWindow,
    setGroup,
    onCommand(handler: (method: string, params: any, target: any) => any) {
      commandHandler = handler;
    },
    failAttach(message: string | null) {
      attachError = message;
    },
    onPageScript(handler: (tabId: number, func: () => unknown) => unknown) {
      pageScript = handler;
    },
  };
}

export type FakeChrome = ReturnType<typeof installFakeChrome>;
