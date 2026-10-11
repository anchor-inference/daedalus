// The pages an agent showed the operator (its PageShow calls), as cards in the conversation: the
// page's icon, its title in the reader's language, which version opens, and Open, which goes to the
// page's screen at /app/p/<slug>. The card asks the host for the page rather than trusting the call's
// words, so a page renamed, republished or removed since is drawn as it is now.

import { Icon, isIconName } from "./icons";
import { t, useLang } from "./i18n";
import { useQuery } from "./store";
import { navigate, pagePath } from "./router";
import type { PageDetail } from "./api";
import type { ShownPage } from "./turnfiles";

/** The title in the reader's language: one per language when the page has them, else the one it has. */
export function pageTitle(title: string | Record<string, string> | undefined, lang: string, slug: string): string {
  if (!title) return slug;
  if (typeof title === "string") return title;
  return title[lang] || title.en || Object.values(title)[0] || slug;
}

/** Where a shown page opens: its screen, on the draft when the agent showed the draft. */
export function shownPath(page: ShownPage): string {
  return pagePath(page.slug) + (page.draft ? "?draft=1" : "");
}

function PageGlyph({ icon, iconPath }: { icon: string | null | undefined; iconPath: string | null | undefined }) {
  if (iconPath) {
    // A page's own glyph: one path on the app's 24-unit grid, stroked like the app's icons (the host
    // accepts only path commands and numbers in it, so it carries no markup).
    return (
      <svg className="ic" viewBox="0 0 24 24" width={18} height={18} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d={iconPath} />
      </svg>
    );
  }
  return <Icon name={isIconName(icon) ? icon : "grid"} size={18} />;
}

export function PageCard({ page }: { page: ShownPage }) {
  const { data, error } = useQuery<PageDetail>(`/api/pages/${encodeURIComponent(page.slug)}`, { staleMs: 30000 });
  const [lang] = useLang();
  const gone = !data && error !== null && /404|no page/i.test(error);
  const title = gone ? page.slug : pageTitle(data?.title, lang, page.slug);
  const side = page.draft
    ? t("pagecard.draft")
    : data
      ? data.version === null ? t("pagecard.unpublished") : t("pagecard.version", { n: data.version })
      : "";
  const meta = gone ? t("pagecard.gone") : [t("pagecard.kind"), side].filter(Boolean).join(" · ");
  const open = () => navigate(shownPath(page));
  return (
    <div className="artifact page-card" data-page={page.slug}>
      <button type="button" className="artifact-main" onClick={open} disabled={gone} title={t("pagecard.open")}>
        <span className="artifact-glyph" aria-hidden><PageGlyph icon={data?.icon} iconPath={data?.icon_path} /></span>
        <span className="artifact-text">
          <span className="artifact-name truncate">{title}</span>
          <span className="artifact-meta truncate">{meta}</span>
        </span>
      </button>
      <span className="artifact-actions">
        <button type="button" className="btn small primary page-card-open" onClick={open} disabled={gone}>{t("pagecard.open")}</button>
      </span>
    </div>
  );
}

/** Every page the turn showed, once each, under the answer. Nothing is drawn for a turn that showed none. */
export function PageCards({ pages }: { pages: ShownPage[] }) {
  if (!pages.length) return null;
  return (
    <div className="artifacts page-cards" aria-label={t("pagecard.list")}>
      {pages.map((page) => <PageCard key={page.slug} page={page} />)}
    </div>
  );
}
