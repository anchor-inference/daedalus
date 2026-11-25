// A row of tabs over one area: the right panel's tools ("panel"), a page's views ("page"). A tab can
// carry an icon, a count and, in the panel, a close button. The variant's class is `tabs-panel`, not
// `panel`: that is the right panel's own class, which starts transparent until it is shown.

import { Tabs as Base } from "@base-ui/react/tabs";
import type { ReactNode } from "react";
import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";

/** `onPress` runs on every press, the chosen tab's too: a tab that opens something over the page (the
 *  project's More) has to answer when it is already the chosen one, which `onChange` never does.
 *  `expanded` marks such a tab as the opener of a dialog and says whether it is open. */
export type TabItem = { id: string; label: ReactNode; icon?: IconName; count?: ReactNode; onClose?: () => void; onPress?: () => void; expanded?: boolean };

export function Tabs({ value, onChange, label, tabs, variant, className = "", children }: { value: string; onChange: (id: string) => void; label: string; tabs: TabItem[]; variant: "panel" | "page"; className?: string; children?: ReactNode }) {
  return (
    <Base.Root value={value} onValueChange={(next) => { if (typeof next === "string" && next !== value) onChange(next); }} className={`tabs tabs-${variant} ${className}`.trim()}>
      <Base.List className="tabs-list" aria-label={label}>
        {tabs.map((tab) => (
          <Base.Tab key={tab.id} value={tab.id} className={`tab ${tab.id === value ? "on" : ""}`} data-tab={tab.id}
            onClick={tab.onPress}
            aria-haspopup={tab.expanded === undefined ? undefined : "dialog"} aria-expanded={tab.expanded}
            onKeyDown={tab.onClose ? (e) => { if (e.key === "Delete") { e.preventDefault(); tab.onClose?.(); } } : undefined}>
            {tab.icon && <Icon name={tab.icon} size={16} />}
            <span className="tab-label">{tab.label}</span>
            {tab.count !== undefined && tab.count !== null && <span className="tab-count">{tab.count}</span>}
            {tab.onClose && (
              // A span, not a button: a button inside the tab's button is not allowed, and the tab
              // keeps the keyboard; Delete on a focused tab closes it as well.
              <span role="button" tabIndex={-1} className="tab-close" aria-label={t("common.close")} title={t("common.close")}
                onClick={(e) => { e.stopPropagation(); tab.onClose?.(); }}>
                <Icon name="close" size={12} />
              </span>
            )}
          </Base.Tab>
        ))}
      </Base.List>
      {children}
    </Base.Root>
  );
}
