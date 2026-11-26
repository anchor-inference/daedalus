// Where a destination sits, changed from the places that show it: the sidebar's right click, and the
// More launcher's pin. One write through the same settings route as Settings → Navigation, with the
// answer handed to the store at once so the sidebar redraws without a reload.
//
// The writes are queued one behind another. Each reads the settings revision it must be based on, so
// two pins pressed in quick succession would race: the second read the old revision, and the host
// refused it as a stale write.

import { api, type Settings } from "./api";
import { type NavConfig, type ResolvedNav, desktopLayout, placeDesktop } from "./navconfig";
import { prime } from "./store";
import { NAV_KEY } from "./usenav";
import { toast } from "./ui/dialogs";
import { errorText } from "./ui";

let queue: Promise<unknown> = Promise.resolve();

/** Runs `write` after the writes already waiting, whether they succeeded or not. */
function enqueue<T>(write: () => Promise<T>): Promise<T> {
  const next = queue.then(write, write);
  queue = next.catch(() => undefined);
  return next;
}

async function save(change: (previous: NavConfig) => NavConfig): Promise<boolean> {
  try {
    const settings = await api.get<Settings>("/api/settings");
    const previous: NavConfig = settings.navigation ?? {};
    const navigation = { drawer: [], icons: {}, ...previous, ...change(previous) };
    const next = await api.put<Settings>("/api/settings", { navigation, base_revision: settings.revision });
    if (next.navigation) prime(NAV_KEY, next.navigation);
    return true;
  } catch (e) {
    toast(errorText(e));
    return false;
  }
}

/** Moves one destination to the sidebar's pinned rows (at their end), to the More launcher, or out of
 *  sight. Resolves to whether the host took it.
 *
 *  The layout is worked out from the configuration as the host holds it when the write comes up, not
 *  from the `nav` the caller drew: a second pin pressed before the first was answered started from the
 *  list without the first, and wrote it away. `nav` only says which destinations exist. */
export function placeItem(nav: ResolvedNav, id: string, to: "pinned" | "more" | "hidden"): Promise<boolean> {
  const known = new Set(nav.catalogue.map((it) => it.id));
  return enqueue(() => save((previous) => {
    const now = placeDesktop(nav.catalogue, previous);
    const placement: Record<string, "pinned" | "more" | "hidden"> = {};
    const order: string[] = [];
    for (const it of now.pinned) { placement[it.id] = "pinned"; order.push(it.id); }
    for (const it of now.more.flatMap((g) => g.items)) { placement[it.id] = "more"; order.push(it.id); }
    for (const it of nav.catalogue) if (now.hidden.has(it.id)) { placement[it.id] = "hidden"; order.push(it.id); }
    placement[id] = to;
    return desktopLayout(previous, order, placement, known);
  }));
}
