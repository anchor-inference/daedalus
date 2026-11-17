/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

/** The page kit, built beside the app: `dist/kit/kit.css` is the app's own tokens.css followed by
 *  kit/kit.css, and `dist/kit/daedalus.js` is the bridge. Made from the sources on every build, so
 *  the tokens a page reads can never fall behind the ones the app paints with; the host serves them
 *  at /pages/_kit/. */
function pageKit(): Plugin {
  const from = (path: string) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");
  return {
    name: "daedalus-page-kit",
    apply: "build",
    generateBundle() {
      const tokens = from("./src/ui/tokens.css");
      this.emitFile({ type: "asset", fileName: "kit/kit.css", source: `/* The app's design tokens (src/ui/tokens.css). */\n${tokens}\n${from("./kit/kit.css")}` });
      this.emitFile({ type: "asset", fileName: "kit/daedalus.js", source: from("./kit/daedalus.js") });
    },
  };
}

export default defineConfig({
  plugins: [react(), pageKit()],
  base: "/app/",
  // One build serves the operator's app at /app/ and a live link's page at /v/<token>/, whose files the
  // services door serves at /v/assets/. A chunk loaded later is asked for where the page itself came
  // from, which the page's address says at run time; a stylesheet names its fonts beside itself.
  experimental: {
    renderBuiltUrl(filename, { hostType }) {
      if (hostType === "js") return { runtime: `(location.pathname.startsWith("/v/")?"/v/":"/app/")+${JSON.stringify(filename)}` };
      if (hostType === "css") return { relative: true };
      return undefined;
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    // The terminal sockets go through as upgrades. The Host header is passed on unchanged (no
    // changeOrigin), which is what lets the host's Origin check see the page and the socket agree.
    proxy: { "/api": "http://127.0.0.1:8765", "/pages": "http://127.0.0.1:8765", "/ws": { target: "ws://127.0.0.1:8765", ws: true } },
  },
  // The stylesheet is read as text by density.test.ts; without this the test runner hands every
  // CSS import over as an empty string and a guard over an empty file passes everything.
  test: { css: { include: [/\.css/] } },
});
