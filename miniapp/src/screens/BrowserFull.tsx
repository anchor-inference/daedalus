// One browser group over the whole column, at its own address (`/app/browser/<group>`): the viewer
// for a second monitor, opened from the Browser tab's menu. Everything the tab does, it does; the
// action log sits beside the picture rather than under it, since the window is the browser's alone.

import type { BrowserGroup } from "../api";
import { BrowserPanel } from "../browser/BrowserPanel";
import { useBrowserGroup } from "../browser/data";
import { agentName, domainOf } from "../browser/model";
import { t } from "../i18n";
import { Icon } from "../icons";
import { back, pathFor, sessionPath } from "../router";
import { useMedia } from "../ui/index";

export function BrowserFullScreen({ id, toast }: { id: string; toast: (text: string) => void }) {
  const group = useBrowserGroup(id);
  const wide = useMedia("(min-width: 1024px)");
  return (
    <div className="chat browser-full">
      <div className="chat-head">
        <button className="iconbtn" onClick={() => back(group?.session_id ? sessionPath(group.session_id) : pathFor("agents"))} aria-label={t("shell.back")} title={t("shell.back")}>
          <Icon name="back" />
        </button>
        <div className="grow chat-identity" style={{ minWidth: 0 }}>
          <span className="chat-title truncate">{group ? title(group) : t("common.loading")}</span>
        </div>
      </div>
      {group ? <BrowserPanel group={group} toast={toast} phone={!wide} full /> : <div className="empty">{t("common.loading")}</div>}
    </div>
  );
}

function title(group: BrowserGroup): string {
  const tab = group.tabs.find((x) => x.active) ?? group.tabs[0];
  return t("browser.full.title", { name: agentName(group), domain: domainOf(tab?.url ?? "") || t("browser.blank") });
}
