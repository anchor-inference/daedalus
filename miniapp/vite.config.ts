/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    // The terminal sockets go through as upgrades. The Host header is passed on unchanged (no
    // changeOrigin), which is what lets the host's Origin check see the page and the socket agree.
    proxy: { "/api": "http://127.0.0.1:8765", "/ws": { target: "ws://127.0.0.1:8765", ws: true } },
  },
  // The stylesheet is read as text by density.test.ts; without this the test runner hands every
  // CSS import over as an empty string and a guard over an empty file passes everything.
  test: { css: { include: [/\.css/] } },
});
