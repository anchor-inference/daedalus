// Injected into the agent's tabs only. Three jobs: a thin pulsing border so the operator can tell an
// agent tab at a glance, the agent's cursor where it clicks, types and scrolls, and a report of the
// operator's own input so the daemon hands control to the human.
//
// This file is a classic script, not a module (Chrome injects it with `scripting.executeScript`), so
// it imports nothing and keeps everything inside one function. It may be injected again on every
// navigation start and must notice that it already runs.

(() => {
  const marker = "__daedalusAgentTab";
  const scope = globalThis as unknown as Record<string, unknown>;
  if (scope[marker]) return;
  scope[marker] = true;

  // A custom element name, so that a page's own selectors never match it and `js/page.js` can skip
  // it by tag, and a closed shadow root, so the page's scripts cannot read or restyle what is inside.
  // Pointer events are off: the border must never take a click meant for the page.
  const host = document.createElement("daedalus-agent-border");
  host.setAttribute("aria-hidden", "true");
  host.style.cssText =
    "all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647";
  const shadow = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  // The border breathes so a working tab is told from a stale one; the cursor glides from its last
  // point rather than jumping, and a click leaves a ring. Reduced motion keeps all of it still.
  style.textContent = `
    .frame{position:absolute;inset:0;pointer-events:none;box-shadow:inset 0 0 0 3px rgba(31,111,235,.85),inset 0 0 18px rgba(31,111,235,.35);animation:pulse 2s ease-in-out infinite}
    @keyframes pulse{50%{box-shadow:inset 0 0 0 3px rgba(31,111,235,.55),inset 0 0 26px rgba(31,111,235,.2)}}
    .cursor{position:absolute;left:0;top:0;pointer-events:none;opacity:0;transition:transform .45s cubic-bezier(.22,1,.36,1),opacity .2s;will-change:transform}
    .cursor.on{opacity:1}
    .cursor svg{display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}
    .label{position:absolute;left:18px;top:18px;padding:2px 7px;border-radius:9px;background:rgba(31,111,235,.92);color:#fff;font:600 11px/16px system-ui,sans-serif;white-space:nowrap}
    .cursor.typing .label::after{content:" …"}
    .ring{position:absolute;left:-14px;top:-14px;width:28px;height:28px;border-radius:50%;border:2px solid rgba(31,111,235,.9);opacity:0;pointer-events:none}
    .ring.go{animation:ring .6s ease-out}
    @keyframes ring{from{opacity:1;transform:scale(.3)}to{opacity:0;transform:scale(1.6)}}
    @media (prefers-reduced-motion:reduce){.frame{animation:none}.cursor{transition:opacity .2s}.ring.go{animation:none}}
  `;
  const frame = document.createElement("div");
  frame.className = "frame";
  const cursor = document.createElement("div");
  cursor.className = "cursor";
  cursor.setAttribute("data-daedalus-cursor", "");
  cursor.innerHTML =
    '<div class="ring"></div><svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 2l15 8-6.5 1.6L9 18z" fill="#1f6feb" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg><span class="label">Daedalus</span>';
  shadow.append(style, frame, cursor);
  const ring = cursor.querySelector(".ring") as HTMLElement;
  let idle: ReturnType<typeof setTimeout> | undefined;

  // The point is the daemon's own, in the main frame's viewport in CSS pixels, which is what a fixed
  // box is placed in: no device pixel ratio and no scroll to add.
  const point = (x: number, y: number, click: boolean, typing: boolean) => {
    if (Number.isFinite(x) && Number.isFinite(y)) cursor.style.transform = `translate(${Math.round(x)}px,${Math.round(y)}px)`;
    cursor.classList.add("on");
    cursor.classList.toggle("typing", typing);
    if (click) {
      ring.classList.remove("go");
      void ring.offsetWidth;
      ring.classList.add("go");
    }
    // Gone after a while without an action, so a cursor does not sit over the page the agent left.
    if (idle) clearTimeout(idle);
    idle = setTimeout(() => cursor.classList.remove("on", "typing"), 8000);
  };

  const mount = () => {
    if (!host.isConnected && document.documentElement) document.documentElement.appendChild(host);
  };
  // Injected at navigation start, the new document may not have its root element yet.
  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount, { once: true });

  let lastSent = 0;
  const onInput = (event: Event) => {
    // Script-made events are not the operator. The daemon's own `Input.dispatch*` events are trusted
    // too; the worker tells those apart by when the daemon last dispatched into this tab.
    if (!event.isTrusted) return;
    const now = Date.now();
    if (now - lastSent < 500) return;
    lastSent = now;
    chrome.runtime.sendMessage({ kind: "daedalus-input" }).catch(() => undefined);
  };
  const events = ["pointerdown", "keydown", "wheel"];
  for (const type of events) window.addEventListener(type, onInput, { capture: true, passive: true });

  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message && message.kind === "daedalus-cursor") {
      point(Number(message.x), Number(message.y), !!message.click, !!message.typing);
      return;
    }
    if (!message || message.kind !== "daedalus-border") return;
    if (message.visible === null) {
      // The tab left the agent's scope: take everything back out of the page.
      host.remove();
      for (const type of events) window.removeEventListener(type, onInput, { capture: true });
      scope[marker] = false;
      return;
    }
    // Hidden while the daemon takes a screenshot, the cursor with the border, so the agent sees the
    // page as the page is and never takes its own cursor for something on it. The answer waits for two
    // frames: a style changed but not yet painted was still in the picture (a few blue pixels of the
    // cursor's edge in the end-to-end check), so the relay takes the screenshot only after it.
    host.style.opacity = message.visible ? "1" : "0";
    host.style.visibility = message.visible ? "visible" : "hidden";
    if (message.visible) return;
    // A tab in the background paints no frames at all; the timer answers for it.
    let answered = false;
    const answer = () => {
      if (answered) return;
      answered = true;
      reply({ hidden: true });
    };
    requestAnimationFrame(() => requestAnimationFrame(answer));
    setTimeout(answer, 150);
    return true;
  });
})();
