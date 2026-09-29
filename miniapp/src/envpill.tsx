// Where a thing runs — a terminal, a folder, a member, a project — drawn one way everywhere.

import type { TerminalEnvName } from "./api";
import { t } from "./i18n";
import { Icon } from "./icons";

/**
 * The environment pill: the full word, capitals, the container in the accent colour and the host in
 * amber with a lock. The host is marked, never guarded — amber and a lock, and no extra question
 * (the operator's decision). There is one spelling on every screen: a phone once swapped in "CONT.",
 * which newcomers read as "continue", and the folder sheet drew the same attribute as a lowercase
 * blue chip one screen away from the amber one.
 */
export function EnvPill({ env, tiny = false }: { env: TerminalEnvName; tiny?: boolean }) {
  return (
    <span className={`term-env ${env} ${tiny ? "tiny" : ""}`} title={t(`term.env.title.${env}`)}>
      {env === "host" && <Icon name="lock" size={11} />}
      {t(`term.env.${env}`)}
    </span>
  );
}
