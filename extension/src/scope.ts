// Which tabs the agent may touch. Scope is a set of daemon groups, each shown to the operator as a
// Chrome tab group; a tab is in scope when it is a member here, never because of where it sits. The
// record lives in `chrome.storage.session` so that a service worker Chrome stopped and started again
// still knows which tabs are the agent's, and so that it forgets them all when the browser closes.

export type Member = {
  tab: number;
  group: string;
  // Whether the tab sits in the group's Chrome tab group. A popup the agent opened is a member but
  // cannot be grouped (Chrome keeps tab groups out of popup windows), and must not be read as having
  // been dragged out of a group it was never in.
  grouped: boolean;
  attached: boolean;
  // Whether the operator put the tab in the agent's group (dragged it in, gave it from the side panel
  // or the context menu) rather than the agent opening it. A tab the agent opened may be closed when
  // it can no longer be driven; one the operator gave is theirs and is only ever let go.
  given: boolean;
  // Flat child sessions seen attached inside this tab; a command may address only one of these.
  children: string[];
  opener?: number;
};

export type Group = {
  id: string;
  title: string;
  // The host conversation the group works for, when the daemon or the side panel named it: the panel
  // shows the conversation that owns the active tab by this.
  session?: string;
  color: chrome.tabGroups.Color;
  paused: boolean;
  chromeGroupId?: number;
  windowId?: number;
  // The blank tab Chrome opens with a new window. It is never the agent's, and it is closed once the
  // first real agent tab arrives, so the window does not carry a stray tab the daemon never asked for.
  placeholderTab?: number;
};

const COLORS: chrome.tabGroups.Color[] = ["blue", "purple", "cyan", "green", "orange", "pink", "yellow", "red"];
const KEY = "scope";

export class Scope {
  groups = new Map<string, Group>();
  members = new Map<number, Member>();

  async load(): Promise<void> {
    const stored = (await chrome.storage.session.get(KEY))[KEY] as { groups?: Group[]; members?: Member[] } | undefined;
    this.groups = new Map((stored?.groups ?? []).map((group) => [group.id, group]));
    // A record without the flag reads as given: a tab that might be the operator's must never be
    // closed on a guess, and letting go of one the agent opened costs only a stray tab.
    this.members = new Map(
      (stored?.members ?? []).map((member) => [member.tab, { ...member, given: (member as Partial<Member>).given ?? true }]),
    );
  }

  async save(): Promise<void> {
    await chrome.storage.session.set({ [KEY]: { groups: [...this.groups.values()], members: [...this.members.values()] } });
  }

  ensureGroup(id: string, title?: string, session?: string): Group {
    let group = this.groups.get(id);
    if (!group) {
      group = { id, title: title ?? "", color: COLORS[this.groups.size % COLORS.length], paused: false };
      this.groups.set(id, group);
    } else if (title) {
      group.title = title;
    }
    if (session) group.session = session;
    return group;
  }

  /** The group that works for a conversation, if this Chrome holds one. */
  bySession(session: string): Group | undefined {
    for (const group of this.groups.values()) if (group.session === session) return group;
    return undefined;
  }

  byChromeGroup(chromeGroupId: number): Group | undefined {
    if (chromeGroupId < 0) return undefined;
    for (const group of this.groups.values()) if (group.chromeGroupId === chromeGroupId) return group;
    return undefined;
  }

  tabsOf(groupId: string): Member[] {
    return [...this.members.values()].filter((member) => member.group === groupId);
  }

  add(tab: number, group: string, grouped: boolean, given: boolean, opener?: number): Member {
    const member: Member = { tab, group, grouped, given, attached: false, children: [], ...(opener === undefined ? {} : { opener }) };
    this.members.set(tab, member);
    return member;
  }

  remove(tab: number): Member | undefined {
    const member = this.members.get(tab);
    this.members.delete(tab);
    return member;
  }

  dropGroup(id: string): Member[] {
    const members = this.tabsOf(id);
    for (const member of members) this.members.delete(member.tab);
    this.groups.delete(id);
    return members;
  }
}
