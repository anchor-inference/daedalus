import React from "react";
import { createRoot } from "react-dom/client";
import { ContextMenuHost } from "./ui/context-menu";
import { App } from "./App";
import { QuickChat } from "./quick/QuickChat";
import { SidePanel } from "./sidepanel/SidePanel";
import "./ui/styles.css";
import "./ui/desktop.css";
import "./ui/phone.css";

// Installable as an app: the service worker keeps the shell and the hashed assets; nothing of the API is cached.
if ("serviceWorker" in navigator && window.location.protocol === "https:") {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(() => undefined);
  });
}

const root = createRoot(document.getElementById("root")!);
// The desktop's Quick Chat window loads /app/quick: the box alone, without the app around it. The
// Daedalus extension's side panel frames /app/panel: the conversations of that Chrome alone.
const route = window.location.pathname.replace(/\/+$/, "");
const render = () =>
  root.render(
    <React.StrictMode>
      {route === "/app/quick" ? <QuickChat /> : route === "/app/panel" ? <><SidePanel /><ContextMenuHost /></> : <><App /><ContextMenuHost /></>}
    </React.StrictMode>,
  );

// Inside Telegram the first thing the app asks is who the reader is, and that answer is in the
// initData Telegram's own script provides. Outside it, __tgReady is already settled and the app
// renders in the same tick.
const ready = (window as unknown as { __tgReady?: Promise<void> }).__tgReady;
if (ready) void ready.then(render);
else render();
