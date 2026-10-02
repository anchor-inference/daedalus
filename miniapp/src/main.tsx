import React from "react";
import { createRoot } from "react-dom/client";
import { ContextMenuHost } from "./ui/context-menu";
import { App } from "./App";
import "./ui/styles.css";
import "./ui/desktop.css";

// Installable as an app: the service worker keeps the shell and the hashed assets; nothing of the API is cached.
if ("serviceWorker" in navigator && window.location.protocol === "https:") {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(() => undefined);
  });
}

const root = createRoot(document.getElementById("root")!);
const render = () =>
  root.render(
    <React.StrictMode>
      <App />
      <ContextMenuHost />
    </React.StrictMode>,
  );

// Inside Telegram the first thing the app asks is who the reader is, and that answer is in the
// initData Telegram's own script provides. Outside it, __tgReady is already settled and the app
// renders in the same tick.
const ready = (window as unknown as { __tgReady?: Promise<void> }).__tgReady;
if (ready) void ready.then(render);
else render();
