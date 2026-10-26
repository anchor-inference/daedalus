// A private chat: kept like any chat, but never remembered, learned from or found by search from another
// chat. Chosen when the chat starts and fixed from then on (the host offers no change), so the app only
// asks it once, in the new chat's `+` menu, and marks the chat wherever it is named afterwards.

import { Icon } from "./icons";
import { t } from "./i18n";

/** The field of the create call: set only when private, so an ordinary chat is sent as before. */
export function privateBody(on: boolean): { private?: true } {
  return on ? { private: true } : {};
}

/** Whether a session row or detail is a private chat: `metadata.private` on a row, `private` on a detail. */
export function isPrivate(s: { private?: boolean; metadata?: { [k: string]: unknown } } | null | undefined): boolean {
  return !!s && (s.private === true || s.metadata?.private === true);
}

/** The mark on a row in the sidebar, the drawer and the Chats page: an icon, the words in its label. */
export function PrivateMark({ on, size = 12 }: { on: boolean; size?: number }) {
  if (!on) return null;
  return <span className="private-mark" role="img" aria-label={t("private.mark")} title={t("private.mark")}><Icon name="eyeoff" size={size} /></span>;
}

/** The chip in a chat's header: the icon and the word, since the header has the room to say it. */
export function PrivateBadge({ on }: { on: boolean }) {
  if (!on) return null;
  return <span className="chip private-badge" title={t("private.mark")}><Icon name="eyeoff" size={12} /><span>{t("private.badge")}</span></span>;
}

/** The chip a new chat's composer carries while private is chosen; pressing it makes the chat ordinary again. */
export function PrivateChip({ on, onClear }: { on: boolean; onClear: () => void }) {
  if (!on) return null;
  return (
    <button type="button" className="chip private-badge private-chip" data-private-chip="" onClick={onClear}
      aria-label={`${t("private.menu")}: ${t("private.chip.off")}`} title={t("private.chip.off")}>
      <Icon name="eyeoff" size={12} /><span>{t("private.badge")}</span><Icon name="close" size={12} />
    </button>
  );
}
