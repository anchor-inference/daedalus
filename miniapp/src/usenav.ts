// The navigation as the shell draws it: the server's `[navigation]` section and the published pages,
// read once for the rail, the menus, the drawer and the palette alike.

import { useMemo } from "react";
import type { SelfDevMode } from "./capabilities";
import { lang } from "./i18n";
import { type Counts, type NavConfig, type PageInfo, type ResolvedNav, resolveNav } from "./navconfig";
import { useQuery } from "./store";

export const NAV_KEY = "/api/navigation";
export const PAGES_KEY = "/api/pages";

/** `enabled` is false before sign-in. A host without pages answers 404, which is read as none: the
 *  store does not retry a refusal, so it costs one request a minute rather than a storm. */
export function useNav(selfdev: SelfDevMode, counts: Counts, enabled = true): ResolvedNav {
  const config = useQuery<NavConfig>(enabled ? NAV_KEY : null, { pollMs: 120000, staleMs: 30000 });
  const pages = useQuery<{ pages?: PageInfo[] }>(enabled ? PAGES_KEY : null, { pollMs: 60000, staleMs: 30000 });
  const language = lang();
  const { inbox, changes, services, agents } = counts;
  return useMemo(
    () => resolveNav({ config: config.data, pages: pages.data?.pages, selfdev, counts: { inbox, changes, services, agents } }),
    // The language is a dependency because the titles are translated here, once.
    [config.data, pages.data, selfdev, inbox, changes, services, agents, language],
  );
}
