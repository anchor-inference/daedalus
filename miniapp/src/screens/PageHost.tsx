// A page the agent published, at /app/p/<slug>. Until the host serves pages this says the page is not
// available and names it, so a link or a rail item that points at one lands somewhere that explains.

import { t } from "../i18n";
import { PageHeader } from "../shell";

export function PageHost({ slug }: { slug: string | null }) {
  return (
    <>
      <PageHeader title={t("nav.page")} />
      <div className="screen narrow">
        <div className="empty" data-page-missing={slug ?? ""}>{t("page.missing", { slug: slug ?? "" })}</div>
      </div>
    </>
  );
}
