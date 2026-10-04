import { Component, type ReactNode } from "react";
import { t } from "../i18n";

/** Keep a failed project detail inside its own panel so the surrounding project remains usable. */
export class ContextErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="empty" role="alert">
      <b>{t("project.detailFailed.title")}</b>
      <div>{t("project.detailFailed.body")}</div>
      <button type="button" className="btn" onClick={() => this.setState({ failed: false })}>{t("common.retry")}</button>
    </div>;
  }
}
