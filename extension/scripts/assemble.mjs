// Puts the static half of the extension (manifest, popup page, Chrome's locale files) beside what
// `tsc` compiled into `dist/`, so that `dist/` is the folder Chrome loads unpacked. The type
// declarations of the extension API compile to nothing and leave nothing behind.
import { cpSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
cpSync(join(root, "static"), join(root, "dist"), { recursive: true });
rmSync(join(root, "dist", "chrome.d.js"), { force: true });
