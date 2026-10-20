// Puts the static half of the extension (manifest, the side panel's page, Chrome's locale files) and
// its icons beside what `tsc` compiled into `dist/`, so that `dist/` is the folder Chrome loads
// unpacked. The type declarations of the extension API compile to nothing and leave nothing behind.
import { cpSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ICON_SIZES, iconPng } from "./icons.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
cpSync(join(root, "static"), join(root, "dist"), { recursive: true });
rmSync(join(root, "dist", "chrome.d.js"), { force: true });
for (const size of ICON_SIZES) writeFileSync(join(root, "dist", `icon-${size}.png`), iconPng(size));
