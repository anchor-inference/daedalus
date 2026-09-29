// The daemon's script, run in an isolated world of each document: the page's own scripts cannot see
// it, reach its ref table, or change what it reads. It is evaluated once per world and defines one
// global, __browserd, whose functions the daemon calls with JSON arguments and whose answers come
// back by value.
//
// A frame of another site is a document this world cannot reach; the daemon runs this script again
// in a world of that frame's own, gives it the frame's ref as the prefix of every ref it hands out,
// and splices what it reads under the frame's line. Every coordinate here is in the viewport of the
// window this world lives in; the daemon adds where that window sits in the tab's.
(() => {
  if (globalThis.__browserd) return;

  const refOf = new WeakMap();      // element -> ref
  const byRef = new Map();          // ref -> WeakRef(element)
  const frameOf = new WeakMap();    // iframe element -> frame number
  const humanTyped = new WeakSet(); // fields a person typed into while driving
  // Elements a page script listens on for a click or a press, as Chromium told the daemon
  // (DOMDebugger.getEventListeners): the <div> made clickable with addEventListener, which nothing in
  // the markup gives away. Kept for the document's life; a listener removed later leaves its mark,
  // which costs a ref, never a wrong click.
  const listened = new WeakSet();
  let nextRef = 1;
  let nextFrame = 1;
  // What this world's refs begin with: "" in the tab's own document, the frame's ref ("f2") in a frame
  // of another site, so a ref names its document and the daemon can route an action to it.
  let prefix = "";

  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-[a-z-]+)(\s|$)/i;
  const INTERACTIVE_ROLES = new Set(["button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider", "spinbutton",
    "treeitem", "gridcell"]);
  const LANDMARKS = new Set(["banner", "navigation", "main", "contentinfo", "complementary", "search", "form",
    "region", "dialog", "alertdialog", "alert"]);
  const STRUCTURE = new Set(["heading", "list", "listitem", "table", "row", "cell", "columnheader", "rowheader",
    "img", "group", "iframe", "article", "tablist", "menu", "menubar", "tree", "grid", "status"]);
  const NAME_FROM_CONTENT = new Set(["button", "link", "heading", "cell", "columnheader", "rowheader", "option",
    "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem", "listitem", "checkbox", "radio", "switch",
    "gridcell", "status", "alert"]);
  // A subtree holding one of these already has its control: a wrapper around it that a script listens
  // on, or that shows a pointer, gets no ref of its own, so a card around a link is the link.
  const CONTROLS = "a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,[onclick]," +
    "[contenteditable=''],[contenteditable=true],[tabindex]:not([tabindex^='-']),[role=button],[role=link]," +
    "[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=combobox]," +
    "[role=textbox],[role=searchbox],[role=slider],[role=treeitem]";
  // The controls a page commonly hides behind a styled label: the label is what a person clicks.
  const PROXIED_TYPES = new Set(["checkbox", "radio", "file"]);
  // Where a cut outline says the rest of the page begins.
  const SECTIONS = "main,nav,header,footer,aside,form,section,article,dialog,[role=main],[role=navigation]," +
    "[role=banner],[role=contentinfo],[role=complementary],[role=region],[role=form],[role=search],[role=dialog],h1,h2,h3";
  const SKIP = new Set(["script", "style", "noscript", "template", "head"]);
  const OPTIONS_SHOWN = 8;
  // The most hit tests one outline makes for [covered by …]: on a page of thousands of controls the
  // ones on screen are what matter, and they come first often enough.
  const COVER_CHECKS = 400;
  // Past this many elements the daemon does not ask Chromium for the page's listeners: the answer
  // alone costs more than the snapshot (browser-use draws the same line).
  const LISTENER_ELEMENTS = 10000;

  function collapse(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  function clip(s, n) {
    s = collapse(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function quote(s) {
    return JSON.stringify(s);
  }

  function ownerRefPrefix(el) {
    // The frame an element lives in: "" for this world's own document, "f<k>" inside an iframe.
    const win = el.ownerDocument && el.ownerDocument.defaultView;
    if (!win || win === window) return "";
    const fe = win.frameElement;
    if (!fe) return "";
    return "f" + frameNumber(fe);
  }

  function frameNumber(iframe) {
    let k = frameOf.get(iframe);
    if (!k) {
      k = nextFrame++;
      frameOf.set(iframe, k);
    }
    return k;
  }

  function frameRef(iframe) {
    return prefix + "f" + frameNumber(iframe);
  }

  function ref(el) {
    let r = refOf.get(el);
    if (!r) {
      r = prefix + ownerRefPrefix(el) + "e" + nextRef++;
      refOf.set(el, r);
      byRef.set(r, new WeakRef(el));
    }
    return r;
  }

  function lookup(r) {
    const w = byRef.get(r);
    const el = w && w.deref();
    if (!el || !el.isConnected) {
      byRef.delete(r);
      return null;
    }
    return el;
  }

  function frameByRef(r) {
    if (typeof r !== "string" || !r.startsWith(prefix)) return null;
    const m = /^f(\d+)$/.exec(r.slice(prefix.length));
    if (!m) return null;
    const k = Number(m[1]);
    for (const f of allFrames(document)) {
      if (frameOf.get(f) === k) return f;
    }
    return null;
  }

  function allFrames(doc) {
    const out = [];
    for (const f of doc.querySelectorAll("iframe, frame")) {
      out.push(f);
      try {
        if (f.contentDocument) out.push(...allFrames(f.contentDocument));
      } catch (e) {}
    }
    return out;
  }

  // crossOrigin says whether a frame's document is out of this world's reach: another site's, read
  // by the daemon in a world of the frame's own.
  function crossOrigin(f) {
    try {
      return !f.contentDocument;
    } catch (e) {
      return true;
    }
  }

  function tag(el) {
    return el.localName || "";
  }

  function inputType(el) {
    return (el.getAttribute("type") || "text").toLowerCase();
  }

  function style(el) {
    return el.ownerDocument.defaultView.getComputedStyle(el);
  }

  // up is the element above n, through shadow roots and out of same-origin frames.
  function up(n) {
    const p = n.parentNode;
    if (!p) return null;
    if (p.nodeType === Node.DOCUMENT_FRAGMENT_NODE) return p.host || null;
    if (p.nodeType === Node.DOCUMENT_NODE) {
      try {
        return p.defaultView && p.defaultView.frameElement;
      } catch (e) {
        return null;
      }
    }
    return p;
  }

  function within(n, el) {
    for (; n; n = up(n)) {
      if (n === el) return true;
    }
    return false;
  }

  function role(el) {
    const explicit = collapse(el.getAttribute && el.getAttribute("role")).split(" ")[0];
    if (explicit && explicit !== "none" && explicit !== "presentation") return explicit;
    switch (tag(el)) {
      case "a":
      case "area":
        return el.hasAttribute("href") ? "link" : "";
      case "button":
        return "button";
      case "summary":
        return "button";
      case "input": {
        const t = inputType(el);
        if (t === "hidden") return "";
        if (["button", "submit", "reset", "image"].includes(t)) return "button";
        if (t === "checkbox") return el.getAttribute("role") === "switch" ? "switch" : "checkbox";
        if (t === "radio") return "radio";
        if (t === "range") return "slider";
        if (t === "number") return "spinbutton";
        if (t === "search") return "searchbox";
        if (t === "file") return "button";
        return "textbox";
      }
      case "textarea":
        return "textbox";
      case "select":
        return el.multiple || el.size > 1 ? "listbox" : "combobox";
      case "option":
        return "option";
      case "img":
        return el.getAttribute("alt") === "" ? "" : "img";
      case "h1": case "h2": case "h3": case "h4": case "h5": case "h6":
        return "heading";
      case "nav":
        return "navigation";
      case "main":
        return "main";
      case "header":
        return el.closest("article, aside, main, nav, section") ? "" : "banner";
      case "footer":
        return el.closest("article, aside, main, nav, section") ? "" : "contentinfo";
      case "aside":
        return "complementary";
      case "form":
        return accessibleName(el, "form") ? "form" : "";
      case "section":
        return accessibleName(el, "region") ? "region" : "";
      case "search":
        return "search";
      case "dialog":
        return "dialog";
      case "ul": case "ol": case "menu":
        return "list";
      case "li":
        return "listitem";
      case "table":
        return "table";
      case "tr":
        return "row";
      case "td":
        return "cell";
      case "th":
        return el.getAttribute("scope") === "row" ? "rowheader" : "columnheader";
      case "iframe": case "frame":
        return "iframe";
      case "article":
        return "article";
      case "details":
        return "group";
      case "fieldset":
        return "group";
      case "progress":
        return "progressbar";
    }
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return "textbox";
    return "";
  }

  function textOf(node, depth) {
    // The text a name is computed from: visible text, with the alt of images, not descending forever.
    if (depth > 20) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.data;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node;
    if (!visible(el)) return "";
    if (tag(el) === "img") return el.getAttribute("alt") || "";
    if (tag(el) === "input" && ["button", "submit", "reset"].includes(inputType(el))) return el.value;
    // Inside a name, a part's own label stands for it, as the accessibility tree has it: the icon
    // with aria-label="Close" in a button, the arrow with title="upvote" in a link.
    const label = depth > 0 && el.getAttribute("aria-label");
    if (collapse(label)) return label;
    let out = "";
    const kids = el.shadowRoot ? el.shadowRoot.childNodes : el.childNodes;
    for (const c of kids) out += " " + textOf(c, depth + 1);
    if (depth > 0 && !collapse(out) && el.getAttribute("title")) return el.getAttribute("title");
    return out;
  }

  function labelledBy(el) {
    const ids = collapse(el.getAttribute("aria-labelledby"));
    if (!ids) return "";
    const doc = el.ownerDocument;
    return ids.split(" ").map((id) => {
      const t = doc.getElementById(id);
      return t ? textOf(t, 0) : "";
    }).join(" ");
  }

  function labelFor(el) {
    const parts = [];
    if (el.labels) for (const l of el.labels) parts.push(textOf(l, 0));
    return parts.join(" ");
  }

  function accessibleName(el, r, noContent) {
    let n = labelledBy(el);
    if (collapse(n)) return clip(n, 120);
    n = el.getAttribute("aria-label");
    if (collapse(n)) return clip(n, 120);
    const t = tag(el);
    if (t === "input" || t === "textarea" || t === "select") {
      const it = inputType(el);
      if (t === "input" && ["button", "submit", "reset"].includes(it)) {
        return clip(el.value || (it === "submit" ? "Submit" : it === "reset" ? "Reset" : ""), 120);
      }
      if (t === "input" && it === "image") return clip(el.getAttribute("alt") || "Submit", 120);
      n = labelFor(el);
      if (collapse(n)) return clip(n, 120);
      n = el.getAttribute("title") || el.getAttribute("placeholder");
      // An unlabelled file input is the button Chromium draws for it.
      if (!n && t === "input" && it === "file") n = "Choose File";
      return clip(n || "", 120);
    }
    if (t === "img") return clip(el.getAttribute("alt") || el.getAttribute("title") || "", 120);
    if (t === "iframe" || t === "frame") return clip(el.getAttribute("title") || el.getAttribute("name") || "", 120);
    if (t === "fieldset") {
      const legend = el.querySelector(":scope > legend");
      if (legend) return clip(textOf(legend, 0), 120);
    }
    if (t === "table") {
      const cap = el.querySelector(":scope > caption");
      if (cap) return clip(textOf(cap, 0), 120);
    }
    if (!noContent && NAME_FROM_CONTENT.has(r || role(el))) {
      n = textOf(el, 0);
      if (collapse(n)) return clip(n, 120);
    }
    return clip(el.getAttribute("title") || "", 120);
  }

  function visible(el) {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return false;
    if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true });
    const s = style(el);
    return s.display !== "none" && s.visibility !== "hidden";
  }

  // native says the markup itself makes el something to act on.
  function native(el, r) {
    if (INTERACTIVE_ROLES.has(r)) return true;
    const t = tag(el);
    if (t === "input" || t === "select" || t === "textarea" || t === "button" || t === "summary") return true;
    if (t === "a" && el.hasAttribute("href")) return true;
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return true;
    if (el.hasAttribute("onclick")) return true;
    // Something a keyboard can reach is something a person can act on.
    const ti = el.getAttribute("tabindex");
    return ti !== null && Number(ti) >= 0;
  }

  // pointerTop says el shows the pointer cursor and its parent does not: the cursor is inherited,
  // so only the topmost element of a pointer chain is the thing a person would click.
  function pointerTop(el) {
    if (style(el).cursor !== "pointer") return false;
    const p = up(el);
    return !p || p.nodeType !== Node.ELEMENT_NODE || style(p).cursor !== "pointer";
  }

  // heuristic says el is clickable by what a script made of it — a listener, a pointer cursor — as a
  // control would be: not the page's frame, not a pane that takes a good part of the screen (where a
  // page listens for the clicks it delegates), and not a wrapper around a control of its own.
  function heuristic(el) {
    const t = tag(el);
    if (t === "body" || t === "html" || (t === "label" && el.control)) return false;
    if (!listened.has(el) && !pointerTop(el)) return false;
    const b = el.getBoundingClientRect();
    if (b.width < 1 || b.height < 1) return false;
    const win = el.ownerDocument.defaultView;
    if (b.width * b.height > 0.3 * win.innerWidth * win.innerHeight) return false;
    return !el.querySelector(CONTROLS);
  }

  // hoverMenu says el opens a menu it holds hidden: a child out of sight, placed over the page
  // (absolutely), with links or buttons in it — the drop-down a CSS :hover or a script shows. Without a
  // ref el could not be hovered, and the menu's items would never be seen.
  function hoverMenu(el) {
    const t = tag(el);
    if (t === "body" || t === "html" || !el.firstElementChild) return false;
    let found = false;
    for (const c of el.children) {
      if (visible(c) || !c.querySelector("a[href], button, [role=menuitem]")) continue;
      const pos = style(c).position;
      if (pos === "absolute" || pos === "fixed") {
        found = true;
        break;
      }
    }
    if (!found) return false;
    const b = el.getBoundingClientRect();
    const win = el.ownerDocument.defaultView;
    return b.width >= 1 && b.height >= 1 && b.width * b.height <= 0.1 * win.innerWidth * win.innerHeight;
  }

  // hiddenControl says a checkbox, radio or file input is drawn by something else: hidden, see-through
  // or shrunk to nothing, the way styled ones are, so its label is what takes the click.
  function hiddenControl(c) {
    if (!visible(c)) return true;
    if (c.checkVisibility && !c.checkVisibility({ opacityProperty: true })) return true;
    const b = c.getBoundingClientRect();
    return b.width < 2 || b.height < 2;
  }

  // proxyControl is the hidden control a label stands for, or null.
  function proxyControl(label) {
    const c = label.control;
    if (!c || tag(c) !== "input" || !PROXIED_TYPES.has(inputType(c))) return null;
    return hiddenControl(c) ? c : null;
  }

  function proxiedBy(input) {
    if (!PROXIED_TYPES.has(inputType(input)) || !input.labels || !input.labels.length) return false;
    for (const l of input.labels) {
      if (visible(l) && proxyControl(l) === input) return true;
    }
    return false;
  }

  // who is the element whose role, name and state stand for el: a label's hidden control, else el.
  function who(el) {
    return (tag(el) === "label" && proxyControl(el)) || el;
  }

  function secret(el) {
    if (tag(el) !== "input" && tag(el) !== "textarea" && !(el.isContentEditable)) return false;
    if (humanTyped.has(el)) return true;
    if (tag(el) === "input" && inputType(el) === "password") return true;
    const ac = el.getAttribute("autocomplete") || "";
    return SECRET_AUTOCOMPLETE.test(ac);
  }

  function secretKind(el) {
    if (tag(el) === "input" && inputType(el) === "password") return "password";
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (/password/.test(ac)) return "password";
    if (/one-time-code/.test(ac)) return "one_time_code";
    if (/cc-/.test(ac)) return "payment";
    return humanTyped.has(el) ? "password" : "";
  }

  function states(el, r) {
    const out = [];
    if (r === "heading") {
      const m = /^h([1-6])$/.exec(tag(el));
      const lv = el.getAttribute("aria-level") || (m && m[1]);
      if (lv) out.push("[level=" + lv + "]");
    }
    const ariaChecked = el.getAttribute("aria-checked");
    if (el.checked === true || ariaChecked === "true") out.push("[checked]");
    if (el.indeterminate || ariaChecked === "mixed") out.push("[mixed]");
    if (el.selected === true && r === "option" || el.getAttribute("aria-selected") === "true") out.push("[selected]");
    const exp = el.getAttribute("aria-expanded");
    if (exp === "true" || (tag(el) === "details" && el.open)) out.push("[expanded]");
    if (exp === "false" || (tag(el) === "details" && !el.open)) out.push("[collapsed]");
    if (el.disabled || el.getAttribute("aria-disabled") === "true") out.push("[disabled]");
    if (el.required || el.getAttribute("aria-required") === "true") out.push("[required]");
    if (el === el.ownerDocument.activeElement && el !== el.ownerDocument.body) out.push("[focused]");
    if (secret(el)) out.push("[secret]");
    return out;
  }

  function valueOf(el, r) {
    if (secret(el)) return null;
    const t = tag(el);
    if (t === "select") {
      const o = el.selectedOptions && el.selectedOptions[0];
      return o ? clip(o.label || o.text, 100) : "";
    }
    if (t === "textarea" || (t === "input" && ["textbox", "searchbox", "spinbutton", "slider", "combobox"].includes(r))) {
      return clip(el.value, 100);
    }
    if (el.isContentEditable && el.getAttribute("contenteditable") !== null) return clip(el.innerText, 100);
    return null;
  }

  // optionsOf is a <select>'s first options, so the agent can pick one without opening it.
  function optionsOf(sel) {
    const opts = Array.from(sel.options).filter((o) => !o.hidden);
    const shown = opts.slice(0, OPTIONS_SHOWN).map((o) => clip(o.label || o.text, 40));
    let s = " options=" + JSON.stringify(shown);
    if (opts.length > OPTIONS_SHOWN) s += " +" + (opts.length - OPTIONS_SHOWN) + " more";
    return s;
  }

  function childrenOf(el) {
    if (tag(el) === "slot") return el.assignedNodes({ flatten: true });
    if (el.shadowRoot) return el.shadowRoot.childNodes;
    if (tag(el) === "iframe" || tag(el) === "frame") {
      try {
        const d = el.contentDocument;
        return d && d.body ? [d.body] : [];
      } catch (e) {
        return [];
      }
    }
    return el.childNodes;
  }

  // offset is where an element's document sits in this window's viewport.
  function offset(el) {
    let x = 0, y = 0;
    let win = el.ownerDocument.defaultView;
    while (win && win !== window && win.frameElement) {
      const fe = win.frameElement;
      const r = fe.getBoundingClientRect();
      const s = win.parent.getComputedStyle(fe);
      x += r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft);
      y += r.top + parseFloat(s.borderTopWidth) + parseFloat(s.paddingTop);
      win = win.parent;
    }
    return { x, y };
  }

  function box(el) {
    const r = el.getBoundingClientRect();
    const o = offset(el);
    return { x: r.left + o.x, y: r.top + o.y, w: r.width, h: r.height };
  }

  // contentOrigin is where a frame's own viewport begins in this window's: inside its border and
  // padding.
  function contentOrigin(f) {
    const b = box(f);
    const s = style(f);
    return { x: b.x + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft),
      y: b.y + parseFloat(s.borderTopWidth) + parseFloat(s.paddingTop) };
  }

  // topAt is the element a click at (x, y) of this window lands on: through same-origin frames and
  // open shadow roots. A frame of another site is where it stops.
  function topAt(x, y) {
    let target = document.elementFromPoint(x, y);
    let px = x, py = y;
    for (;;) {
      if (target && (tag(target) === "iframe" || tag(target) === "frame")) {
        let inner = null;
        try {
          inner = target.contentDocument;
        } catch (e) {}
        if (!inner) break;
        const o = contentOrigin(target);
        px = x - o.x;
        py = y - o.y;
        const next = inner.elementFromPoint(px, py);
        if (!next) break;
        target = next;
        continue;
      }
      if (target && target.shadowRoot && target.shadowRoot.elementFromPoint) {
        const inner = target.shadowRoot.elementFromPoint(px, py);
        if (!inner || inner === target) break;
        target = inner;
        continue;
      }
      break;
    }
    return target;
  }

  // samples are the points of a visible box a hit test tries: its centre first, then four around it.
  function samples(x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, dx = (x1 - x0) / 4, dy = (y1 - y0) / 4;
    return [[cx, cy], [cx - dx, cy - dy], [cx + dx, cy - dy], [cx - dx, cy + dy], [cx + dx, cy + dy]];
  }

  // onScreen is the part of el's box inside the viewport, or null.
  function onScreen(el) {
    const b = box(el);
    const x0 = Math.max(b.x, 0), y0 = Math.max(b.y, 0);
    const x1 = Math.min(b.x + b.w, innerWidth), y1 = Math.min(b.y + b.h, innerHeight);
    return x1 - x0 < 1 || y1 - y0 < 1 ? null : [x0, y0, x1, y1];
  }

  // coverer names what lies over el at a point where t was hit: the layer t is part of — the
  // outermost element above t that does not also hold el (a landmark around both says nothing) —
  // by its role and name when it has them, else by its id or class and its first words.
  function coverer(t, el) {
    let layer = t;
    for (let n = up(t), i = 0; n && i < 40; n = up(n), i++) {
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      if (within(el, n) || tag(n) === "body" || tag(n) === "html") break;
      layer = n;
    }
    const r = role(layer);
    if (LANDMARKS.has(r) || native(layer, r) || STRUCTURE.has(r)) {
      const nm = accessibleName(layer, r);
      return (r || tag(layer)) + (nm ? " " + quote(clip(nm, 60)) : "");
    }
    let s = tag(layer);
    if (layer.id) {
      s += "#" + clip(layer.id, 30);
    } else if (typeof layer.className === "string" && layer.className.trim()) {
      s += "." + clip(layer.className.trim().split(/\s+/)[0], 30);
    }
    const words = clip(textOf(layer, 0), 40);
    return words ? s + " " + quote(words) : s;
  }

  // coveredBy is what covers el on screen, or "": a hit test at the centre of its visible part and,
  // when that misses, at four points around it. A point that lands on an ancestor (a gap in a link
  // that wraps, a control that lets clicks through) proves nothing and is not held against it.
  function coveredBy(el) {
    const v = onScreen(el);
    if (!v) return "";
    let first = null;
    for (const [x, y] of samples(...v)) {
      const t = topAt(x, y);
      if (!t || within(t, el) || within(el, t)) return "";
      if (!first) first = t;
    }
    return first ? coverer(first, el) : "";
  }

  // outline walks from root (an element, or a list of nodes read in turn) and returns its lines, the
  // frames it met, and where it stopped when it ran out of room. opts.band limits it to elements
  // that reach into [band[0], band[1]] of the viewport's height; opts.marks adds [covered by …].
  function outline(root, maxChars, opts) {
    opts = opts || {};
    const band = opts.band || null;
    const lines = [];
    const frames = [];
    let used = 0;
    let truncated = false;
    let cutAt = null;
    let refs = 0;
    let checks = 0;
    function emit(depth, line, el) {
      if (truncated) return false;
      const s = "  ".repeat(depth) + "- " + line;
      if (used + s.length + 1 > maxChars) {
        truncated = true;
        cutAt = el;
        return false;
      }
      used += s.length + 1;
      lines.push({ s, el });
      return true;
    }
    function outside(el) {
      const b = box(el);
      return b.w > 0 && b.h > 0 && (b.y + b.h < band[0] || b.y > band[1]);
    }
    function walk(node, depth, inRef) {
      if (truncated || depth > 60) return;
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const t = tag(el);
      if (SKIP.has(t)) return;
      if (!visible(el)) return;
      if (band && outside(el)) return;
      // A control its label stands for is read on the label's line.
      if (t === "input" && proxiedBy(el)) return;
      const proxy = t === "label" ? proxyControl(el) : null;
      const w = proxy || el;
      const r = role(w);
      const kind = proxy || native(el, r) ? "native" : !inRef && heuristic(el) ? "heuristic" :
        !inRef && hoverMenu(el) ? "menu" : "";
      const isInteractive = kind !== "";
      // A pane that scrolls on its own (a sidebar, a feed, a carousel) gets a line and a ref, so it can
      // be scrolled by its ref; the window's own scroll is in the header.
      const pane = !isInteractive && t !== "body" && t !== "html" && ownScroll(el);
      const significant = isInteractive || pane || LANDMARKS.has(r) || STRUCTURE.has(r) || r === "textbox";
      let childDepth = depth;
      let cross = false;
      if (significant) {
        // A cell or a list item that is not a control is not named by its words: they are on the
        // lines under it, and a table would say every one of them twice.
        let name = accessibleName(w, r, !isInteractive && r !== "heading" && r !== "iframe");
        let named = false;
        if (kind === "heuristic" && !name) {
          // A clickable <div> is named by its words when they are few, as a button would be.
          const words = collapse(textOf(el, 0));
          if (words && words.length <= 100) {
            name = words;
            named = true;
          }
        }
        if (kind === "menu" && !name) name = clip(textOf(el, 0), 100);
        let line = (r || (isInteractive || pane ? "generic" : t)) + (name ? " " + quote(name) : "");
        if (isInteractive || pane || r === "iframe") {
          line += " [ref=" + (r === "iframe" ? frameRef(el) : ref(el)) + "]";
        }
        const st = states(w, r);
        if (pane) st.push("[scrollable]");
        if (kind === "menu") st.push("[collapsed]");
        if (st.length) line += " " + st.join(" ");
        const v = valueOf(w, r);
        if (v !== null && v !== "") line += " value=" + quote(v);
        if (tag(w) === "select") line += optionsOf(w);
        if (r === "link" && w.href) line += " url=" + quote(clip(w.href, 200));
        // A frame's address, so a frame that cannot be read can still be opened in a tab of its own.
        if (r === "iframe" && el.src) line += " url=" + quote(clip(el.src, 200));
        if (isInteractive && opts.marks && checks < COVER_CHECKS) {
          checks++;
          const c = coveredBy(el);
          if (c) line += " [covered by " + c + "]";
        }
        if (r === "iframe") cross = crossOrigin(el);
        const at = lines.length;
        if (!emit(depth, line, el)) return;
        if (isInteractive || pane || r === "iframe") refs++;
        if (r === "iframe") frames.push({ ref: frameRef(el), url: el.src || "", cross_origin: cross, line: at, depth });
        childDepth = depth + 1;
        if (proxy || named) return;
        // A named control's own text is its name: not repeated underneath it.
        if (isInteractive && r !== "iframe" && NAME_FROM_CONTENT.has(r)) return;
        if (r === "heading" || r === "img") return;
      }
      // A field's content is its value, which the line above shows or, for a secret, withholds.
      if (t === "textarea" || t === "input" || t === "select" || cross) return;
      // A label's words are its control's name, already on the control's line.
      children(childrenOf(el), childDepth, inRef || isInteractive, t === "label" && !!el.control);
    }
    function children(kids, depth, inRef, labelsAControl) {
      let text = "";
      let textEl = null;
      const flush = () => {
        const s = collapse(text);
        if (s && !labelsAControl) emit(depth, "text " + quote(clip(s, 300)), textEl);
        text = "";
        textEl = null;
      };
      for (const c of kids) {
        if (truncated) return;
        if (c.nodeType === Node.TEXT_NODE) {
          text += " " + c.data;
          // The node itself, not its parent: where a cut falls is compared with what follows it,
          // and a parent holds what came before as well.
          textEl = textEl || c;
          continue;
        }
        if (c.nodeType === Node.ELEMENT_NODE && isInline(c) && !significantish(c, inRef)) {
          text += " " + textOf(c, 0);
          textEl = textEl || c;
          continue;
        }
        flush();
        walk(c, depth, inRef);
      }
      flush();
    }
    if (Array.isArray(root)) {
      children(root, 0, false, false);
    } else {
      walk(root, 0, false);
    }
    return { lines, frames, truncated, refs, used, cutAt };
  }

  const INLINE = new Set(["span", "b", "i", "em", "strong", "small", "code", "abbr", "cite", "q", "sub", "sup",
    "time", "mark", "u", "s", "kbd", "var", "br", "wbr", "bdi", "bdo", "data", "font"]);

  function isInline(el) {
    return INLINE.has(tag(el));
  }

  // significantish says an inline element is read as an element, not flattened into its parent's
  // text: it is a control, or holds one (a <span> around a link, which flattening used to swallow).
  function significantish(el, inRef) {
    const r = role(el);
    if (native(el, r) || LANDMARKS.has(r) || STRUCTURE.has(r)) return true;
    if (tag(el) === "label" && proxyControl(el)) return true;
    if (el.querySelector(CONTROLS)) return true;
    return !inRef && heuristic(el);
  }

  function round1(n) {
    return Math.round(n * 10) / 10;
  }

  function screens(n) {
    return n + (n === 1 ? " screen" : " screens");
  }

  function scrollableY(n) {
    if (n.nodeType !== Node.ELEMENT_NODE) return false;
    return /(auto|scroll|overlay)/.test(style(n).overflowY) && n.scrollHeight > n.clientHeight + 1;
  }

  function scrollableX(n) {
    if (n.nodeType !== Node.ELEMENT_NODE) return false;
    return /(auto|scroll|overlay)/.test(style(n).overflowX) && n.scrollWidth > n.clientWidth + 1;
  }

  // ownScroll says el scrolls its own content by more than a line, either way. The sizes are read
  // first: they are known after layout, while the style is computed for the few that overflow.
  function ownScroll(el) {
    const y = el.scrollHeight > el.clientHeight + 20 && el.clientHeight >= 40;
    const x = el.scrollWidth > el.clientWidth + 20 && el.clientWidth >= 40;
    if (!y && !x) return false;
    const s = style(el);
    return (y && /(auto|scroll|overlay)/.test(s.overflowY)) || (x && /(auto|scroll|overlay)/.test(s.overflowX));
  }

  // scrollState is how far the page is scrolled and how much of it is left: the window's scroll, or,
  // on a page whose window does not scroll (an app that scrolls a pane), the pane's at the centre.
  function scrollState() {
    const se = document.scrollingElement || document.documentElement;
    let top = se ? se.scrollTop : 0, height = se ? se.scrollHeight : innerHeight, view = innerHeight, el = null;
    if (height <= view + 1) {
      for (let n = document.elementFromPoint(innerWidth / 2, innerHeight / 2); n && n !== se; n = up(n)) {
        if (n.ownerDocument === document && scrollableY(n)) {
          el = n;
          top = n.scrollTop;
          height = n.scrollHeight;
          view = n.clientHeight;
          break;
        }
      }
    }
    view = Math.max(view, 1);
    return { top: Math.round(top), height: Math.round(height), view: Math.round(view),
      above: round1(Math.max(0, top) / view), below: round1(Math.max(0, height - top - view) / view), el };
  }

  function headerLine(st, loading) {
    const parts = ["viewport " + innerWidth + "x" + innerHeight];
    if (st.el) {
      const r = role(st.el);
      const nm = accessibleName(st.el, r);
      parts.push("the page scrolls inside " + (r || tag(st.el)) + (nm ? " " + quote(clip(nm, 60)) : "") + " [ref=" + ref(st.el) + "]");
    }
    if (st.height <= st.view + 1) {
      parts.push("the page fits");
    } else {
      parts.push(st.above < 0.05 ? "at the top" : screens(st.above) + " above");
      parts.push(st.below < 0.05 ? "at the bottom" : screens(st.below) + " below");
    }
    if (loading) parts.push("still loading");
    return "- [" + parts.join(", ") + "]";
  }

  function headingLevel(h) {
    const m = /^h([1-6])$/.exec(tag(h));
    return m ? Number(m[1]) : Number(h.getAttribute("aria-level")) || 2;
  }

  // sectionOf is a heading's section: the heading and what follows it up to the next heading of its
  // level or above, wherever the page nests them.
  function sectionOf(h) {
    const level = headingLevel(h);
    const stop = [1, 2, 3, 4, 5, 6].filter((i) => i <= level).map((i) => "h" + i).join(",");
    const out = [h];
    let n = h;
    for (;;) {
      while (n && !n.nextSibling) {
        n = n.parentNode;
        if (!n || n === document.body || n === document.documentElement || n.nodeType !== Node.ELEMENT_NODE) return out;
      }
      if (!n) return out;
      n = n.nextSibling;
      // Into what holds the next heading, so the section stops just before it.
      while (n && n.nodeType === Node.ELEMENT_NODE && !n.matches(stop) && n.querySelector(stop)) n = n.firstChild;
      if (!n || (n.nodeType === Node.ELEMENT_NODE && n.matches(stop))) return out;
      out.push(n);
      if (out.length > 5000) return out;
    }
  }

  function sectionLabel(el) {
    const r = role(el);
    let nm = accessibleName(el, r);
    if (!nm && !/^h[1-6]$/.test(tag(el))) {
      const h = el.querySelector("h1, h2, h3, h4, h5, h6");
      if (h) nm = clip(textOf(h, 0), 60);
    }
    return (r || tag(el)) + (nm ? " " + quote(clip(nm, 60)) : "");
  }

  // notReached lists where the page goes on after the cut: the rest of the section the cut fell in,
  // then the landmarks and headings it did not reach, each with a ref to take a snapshot of (a
  // heading's reads its section).
  function notReached(root, cutAt, first) {
    const out = [];
    if (!cutAt || Array.isArray(root)) return out;
    // The section the cut fell in, when the outline showed its start and not the start of the page
    // with it: a <main> holding everything would only read the page again.
    let last = null;
    for (let n = cutAt.nodeType === Node.ELEMENT_NODE ? cutAt : cutAt.parentElement; n && n !== root; n = n.parentElement) {
      if (n.matches && n.matches(SECTIONS) && !/^h[1-6]$/.test(tag(n))) {
        if (!(first && within(first, n))) {
          out.push("the rest of " + sectionLabel(n) + " [ref=" + ref(n) + "]");
          last = n;
        }
        break;
      }
    }
    for (const el of root.querySelectorAll(SECTIONS)) {
      if (el !== cutAt && !(cutAt.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
      if (cutAt !== el && el.contains && el.contains(cutAt)) continue;
      if (last && last.contains(el)) continue;
      if (!visible(el)) continue;
      out.push(sectionLabel(el) + " [ref=" + ref(el) + "]");
      if (!/^h[1-6]$/.test(tag(el))) last = el;
      if (out.length >= 40) break;
    }
    return out;
  }

  // cut makes a cut outline end well: it keeps the start, the focused element's region when the cut
  // left it out, and a line naming where the page goes on, as much of it as fits.
  function cut(o, root, maxChars, taken) {
    const room = Math.min(6000, Math.max(400, Math.floor(maxChars / 4)));
    const first = o.lines.length ? o.lines[0].el : null;
    while (o.lines.length && taken + o.used > maxChars - room) {
      const l = o.lines.pop();
      o.used -= l.s.length + 1;
      o.cutAt = l.el || o.cutAt;
    }
    o.frames = o.frames.filter((f) => f.line < o.lines.length);
    const tail = [];
    const f = deepFocus();
    const after = (el) => o.cutAt && (el === o.cutAt ||
      (o.cutAt.compareDocumentPosition(el) & (Node.DOCUMENT_POSITION_FOLLOWING | Node.DOCUMENT_POSITION_CONTAINED_BY)));
    let left = room;
    if (f && after(f) && (Array.isArray(root) || within(f, root))) {
      const region = f.closest("form, dialog, [role=dialog], main, nav, aside, section, article, [role=region], " +
        "[role=form], [role=search], fieldset, li, tr") || f.parentElement || f;
      const r = outline(region, Math.max(200, Math.floor(left * 0.5)), { marks: true });
      if (r.lines.length) {
        tail.push("- [the focused element's region:]");
        for (const l of r.lines) tail.push("  " + l.s);
        left -= r.used + 2 * r.lines.length + 40;
        o.refs += r.refs;
      }
    }
    const head = "- [the outline was cut at " + maxChars + " characters";
    const end = "; take a snapshot with scope=<ref> for one part]";
    let line = head;
    const rest = notReached(root, o.cutAt, first);
    for (let i = 0; i < rest.length; i++) {
      const item = (i === 0 ? "; not shown: " : ", ") + rest[i];
      if (line.length + item.length + end.length + 3 > left) {
        line += i === 0 ? "" : ", …";
        break;
      }
      line += item;
    }
    tail.push(line + end);
    return tail;
  }

  function snapshot(scope, maxChars, opts) {
    opts = opts || {};
    let root = document.body || document.documentElement;
    if (scope) {
      root = lookup(scope) || frameByRef(scope);
      if (!root) return { error: "stale", ref: scope };
      // A heading's scope is its section.
      if (/^h[1-6]$/.test(tag(root)) || role(root) === "heading") root = sectionOf(root);
    }
    const viewport = opts.view === "viewport";
    const st = scrollState();
    const loading = document.readyState !== "complete";
    const header = [];
    if (opts.header) {
      header.push(headerLine(st, loading));
      if (viewport) header.push(st.above < 0.05 ? "- [start of page]" : "- [" + screens(st.above) + " above; scroll up to see them]");
    }
    let taken = 0;
    for (const h of header) taken += h.length + 1;
    const band = viewport ? (opts.band || [-0.5 * innerHeight, 1.5 * innerHeight]) : null;
    const reserve = opts.header ? 200 : 0;
    const o = outline(root, Math.max(100, maxChars - taken - reserve), { band, marks: opts.marks !== false });
    let tail = [];
    if (o.truncated) {
      tail = cut(o, root, maxChars, taken);
    } else if (opts.header) {
      if (viewport && st.below >= 0.05) {
        tail.push("- [" + screens(st.below) + " below; scroll down to see them]");
      } else {
        tail.push("- [end of page]");
      }
    }
    if (opts.header && !scope && o.refs === 0 && o.lines.length <= 2) {
      tail.push("- [the page looks empty" + (loading ? " and is still loading" : "") +
        "; wait for it to load, or look at a screenshot: it may draw on a canvas]");
    }
    return {
      url: location.href, title: document.title, header, lines: o.lines.map((l) => l.s), tail, refs: o.refs,
      truncated: o.truncated, frames: o.frames, used: taken + o.used,
      viewport: { w: innerWidth, h: innerHeight },
      scroll: { top: st.top, height: st.height, view: st.view, above: st.above, below: st.below },
      loading,
    };
  }

  // readable is the page's main text, as a reader view would take it: the main landmark or the
  // article, else the body without its navigation, header and footer.
  function readable(scope, maxChars) {
    let root;
    if (scope) {
      root = lookup(scope);
      if (!root) return { error: "stale", ref: scope };
    } else {
      root = document.querySelector("main, [role=main], article") || document.body;
    }
    if (!root) return { url: location.href, title: document.title, text: "", truncated: false };
    const skip = new Set(["nav", "header", "footer", "aside", "script", "style", "noscript", "template"]);
    const parts = [];
    function walk(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        parts.push(node.data);
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      if (!visible(el) || (el !== root && skip.has(tag(el)))) return;
      if (secret(el)) return;
      const block = !isInline(el);
      if (block) parts.push("\n");
      if (tag(el) === "img" && el.alt) parts.push("[" + el.alt + "]");
      for (const c of childrenOf(el)) walk(c);
      if (block) parts.push("\n");
    }
    walk(root);
    let text = parts.join("").replace(/[ \t\f\v\r]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    let truncated = false;
    if (text.length > maxChars) {
      text = text.slice(0, maxChars);
      truncated = true;
    }
    return { url: location.href, title: document.title, text, truncated };
  }

  // blocks calls fn(element, text) for each run of text the outline would show as one line, in
  // document order, until fn returns true. Secret fields and hidden text are not read.
  function blocks(root, fn) {
    let stop = false;
    function walk(el) {
      if (stop || SKIP.has(tag(el)) || !visible(el) || secret(el)) return;
      let text = "";
      const flush = () => {
        const s = collapse(text);
        text = "";
        if (s && fn(el, s)) stop = true;
      };
      for (const c of childrenOf(el)) {
        if (stop) return;
        if (c.nodeType === Node.TEXT_NODE) {
          text += " " + c.data;
        } else if (c.nodeType === Node.ELEMENT_NODE) {
          if (isInline(c) && !significantish(c, true)) {
            text += " " + textOf(c, 0);
          } else {
            flush();
            walk(c);
          }
        }
      }
      flush();
    }
    if (root) walk(root);
  }

  // controlOf is the control an element belongs to: itself or an ancestor the markup, a listener or a
  // label makes one, else the top of the pointer chain it is part of, else null.
  function controlOf(el) {
    let pointer = null;
    for (let n = el, i = 0; n && i < 8; n = up(n), i++) {
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      if (tag(n) === "body" || tag(n) === "html") break;
      if (native(n, role(n)) || listened.has(n) || (tag(n) === "label" && proxyControl(n))) return n;
      if (style(n).cursor === "pointer") pointer = n;
      else if (pointer) break;
    }
    return pointer;
  }

  function about(el) {
    const w = who(el);
    const r = role(w);
    return { ref: ref(el), role: r || "generic", name: accessibleName(w, r) };
  }

  // find is a search of the page's visible text, as a person's Ctrl+F: each match with the words
  // around it and the control it is in, or the few controls beside it (the Add to cart of the
  // product whose name matched).
  function find(query, isRegex, caseSensitive, max) {
    let re;
    try {
      re = new RegExp(isRegex ? query : query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), caseSensitive ? "g" : "gi");
    } catch (e) {
      return { error: "bad_regex", message: String(e && e.message || e) };
    }
    const matches = [];
    let total = 0;
    blocks(document.body || document.documentElement, (el, text) => {
      re.lastIndex = 0;
      let m;
      let here = 0;
      while ((m = re.exec(text)) !== null) {
        if (m[0] === "") {
          re.lastIndex++;
          continue;
        }
        total++;
        if (matches.length < max && here < 3) {
          here++;
          const from = Math.max(0, m.index - 60), to = Math.min(text.length, m.index + m[0].length + 60);
          const match = { text: (from > 0 ? "…" : "") + text.slice(from, to) + (to < text.length ? "…" : "") };
          const own = controlOf(el);
          if (own) {
            match.in = about(own);
          } else {
            for (let n = el, i = 0; n && i < 5; n = n.parentElement, i++) {
              const cs = Array.from(n.querySelectorAll(CONTROLS)).filter((c) => visible(c));
              if (cs.length > 12) break;
              if (cs.length) {
                match.near = cs.slice(0, 3).map(about);
                break;
              }
            }
          }
          matches.push(match);
        }
        if (total > 1000) return true;
      }
      return false;
    });
    return { url: location.href, matches, total };
  }

  function describe(el) {
    const w = who(el);
    const r = role(w);
    const form = w.form || (w.closest && w.closest("form"));
    const out = { role: r || tag(w), name: accessibleName(w, r), tag: tag(el) };
    if (tag(w) === "input") out.type = inputType(w);
    const ac = w.getAttribute("autocomplete");
    if (ac) out.autocomplete = ac;
    if (w.href) out.href = w.href;
    if (form) out.form_action = form.action || location.href;
    out.secret = secret(w);
    out.secret_kind = secretKind(w);
    out.disabled = !!(w.disabled || w.getAttribute("aria-disabled") === "true");
    out.checked = w.checked === true || w.getAttribute("aria-checked") === "true";
    out.file = tag(w) === "input" && inputType(w) === "file";
    out.select = tag(w) === "select";
    return out;
  }

  // prepare brings an element into view and says where it is and what it is.
  function prepare(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    const b0 = box(el);
    const vw = window.innerWidth, vh = window.innerHeight;
    if (b0.x < 0 || b0.y < 0 || b0.x + b0.w > vw || b0.y + b0.h > vh) {
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    }
    const b = box(el);
    return { box: b, element: describe(el), viewport: { w: vw, h: vh } };
  }

  // measure is prepare without the scrolling: for an element found at a point, which is where it is.
  function measure(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    return { box: box(el), element: describe(el), viewport: { w: innerWidth, h: innerHeight } };
  }

  // hit says whether a click at (x, y) lands on the element (or inside it), not on something
  // covering it. When the point is covered and another point of the element is not, that point is
  // given: the click still lands on the named element, only elsewhere on it.
  function hit(r, x, y) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    const target = topAt(x, y);
    if (target && within(target, el)) return { ok: true };
    const v = onScreen(el);
    if (v) {
      for (const [sx, sy] of samples(...v)) {
        const t = topAt(sx, sy);
        if (t && within(t, el)) return { ok: true, x: sx, y: sy };
      }
    }
    return { ok: false, covered_by: target ? coverer(target, el) : "" };
  }

  // elementAt is what a click at (x, y) would act on: the control the element there belongs to, or
  // the element itself; or, over a frame of another site, the frame and the point inside it.
  function elementAt(x, y) {
    const t = topAt(x, y);
    if (!t) return { error: "nothing" };
    if ((tag(t) === "iframe" || tag(t) === "frame") && crossOrigin(t)) {
      const o = contentOrigin(t);
      return { frame: frameRef(t), x: x - o.x, y: y - o.y };
    }
    return { ref: ref(controlOf(t) || t) };
  }

  // hovered says the pointer is over the element, as the page itself sees it.
  function hovered(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    return { hovered: el.matches(":hover") || who(el).matches(":hover") };
  }

  function focus(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    const w = who(el);
    w.focus({ preventScroll: true });
    return { focused: w.ownerDocument.activeElement === w };
  }

  function selectAll(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    el.focus({ preventScroll: true });
    if (typeof el.select === "function" && (tag(el) === "input" || tag(el) === "textarea")) {
      el.select();
    } else if (el.isContentEditable) {
      const sel = el.ownerDocument.getSelection();
      const range = el.ownerDocument.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
    }
    return { ok: true };
  }

  function selectOption(r, option) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    if (tag(el) !== "select") return { error: "not_select" };
    const opts = Array.from(el.options);
    const o = opts.find((x) => collapse(x.label || x.text) === collapse(option)) || opts.find((x) => x.value === option);
    if (!o) return { error: "no_option", options: opts.slice(0, 50).map((x) => collapse(x.label || x.text)) };
    el.focus({ preventScroll: true });
    o.selected = true;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, value: collapse(o.label || o.text) };
  }

  // element is the node an action sets files on: a label's hidden file input rather than the label.
  function element(r) {
    const el = lookup(r);
    return el ? who(el) : null;
  }

  // scroller is the element that scrolls el's content along the axis: el itself or an ancestor, or
  // null for the window.
  function scroller(el, horizontal) {
    for (let n = el; n; n = up(n)) {
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      if (n === document.documentElement || n === document.body) return null;
      if (horizontal ? scrollableX(n) : scrollableY(n)) return n;
    }
    return null;
  }

  // scrollInfo is where the pane that scrolls el is, and how far it is scrolled, for a wheel over it.
  function scrollInfo(r, horizontal) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    const s = scroller(el, horizontal);
    if (!s) {
      const se = document.scrollingElement || document.documentElement;
      return { window: true, top: se.scrollTop, left: se.scrollLeft, height: se.scrollHeight, width: se.scrollWidth,
        view_h: innerHeight, view_w: innerWidth };
    }
    if (!onScreen(s)) s.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
    const v = onScreen(s) || [0, 0, innerWidth, innerHeight];
    return { ref: ref(s), box: { x: v[0], y: v[1], w: v[2] - v[0], h: v[3] - v[1] }, top: s.scrollTop, left: s.scrollLeft,
      height: s.scrollHeight, width: s.scrollWidth, view_h: s.clientHeight, view_w: s.clientWidth };
  }

  // scrollBy moves a pane the wheel did not move: a pane that takes no wheel events still scrolls.
  function scrollBy(r, dx, dy) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    el.scrollBy({ left: dx, top: dy, behavior: "instant" });
    return { top: el.scrollTop, left: el.scrollLeft };
  }

  // scrollToText brings the first visible text containing q to the middle of the viewport.
  function scrollToText(q) {
    const want = collapse(q).toLowerCase();
    let found = null;
    blocks(document.body || document.documentElement, (el, text) => {
      if (text.toLowerCase().includes(want)) {
        found = el;
        return true;
      }
      return false;
    });
    if (!found) return { found: false };
    found.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" });
    return { found: true, ref: ref(controlOf(found) || found), box: box(found) };
  }

  // The words near an element that say what it does: its name and value, the nearest heading, the
  // form's submit control.
  function nearestHeading(el) {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
        if (/^h[1-6]$/.test(tag(s))) return textOf(s, 0);
        const h = s.querySelector && s.querySelector("h1, h2, h3, h4, h5, h6");
        if (h) return textOf(h, 0);
      }
    }
    return "";
  }

  function evidence(r, action, submit) {
    const found = lookup(r);
    if (!found) return { error: "stale", ref: r };
    const el = who(found);
    const d = describe(found);
    const form = el.form || el.closest("form");
    const fields = [];
    let password = false, payment = false, otp = false;
    if (form) {
      for (const f of form.querySelectorAll("input, textarea, select")) {
        const k = secretKind(f);
        if (k === "password") password = true;
        if (k === "payment") payment = true;
        if (k === "one_time_code") otp = true;
        const ac = f.getAttribute("autocomplete") || "";
        if (/cc-/.test(ac)) payment = true;
        if (fields.length < 20) fields.push({ type: tag(f) === "input" ? inputType(f) : tag(f), name: accessibleName(f, role(f)), autocomplete: ac });
      }
    }
    const isSubmitControl = (tag(el) === "button" && (el.getAttribute("type") || "submit").toLowerCase() === "submit" && !!form) ||
      (tag(el) === "input" && ["submit", "image"].includes(inputType(el)));
    const text = collapse(d.name + " " + (el.getAttribute("title") || ""));
    return {
      name: d.name, role: d.role, tag: tag(el), type: d.type || "", text,
      value: tag(el) === "input" && ["submit", "button"].includes(inputType(el)) ? el.value : "",
      heading: clip(nearestHeading(el), 120),
      form: !!form, form_action: form ? (form.action || location.href) : "", form_method: form ? (form.method || "get") : "",
      submits: isSubmitControl || (!!submit && !!form) || (action === "press" && !!form),
      password, payment, otp, file: d.file,
      page_origin: location.origin, fields,
    };
  }

  // Screenshots never show a secret: its text is hidden and a blank box drawn over it, both removed
  // after the capture.
  let masks = [];
  function mask(on) {
    for (const m of masks) m.remove();
    masks = [];
    const hidden = [];
    const restore = globalThis.__browserdRestore || [];
    for (const [el, prev] of restore) el.style.setProperty("-webkit-text-security", prev);
    globalThis.__browserdRestore = [];
    if (!on) return { masked: [] };
    const docs = [document];
    for (const f of allFrames(document)) {
      try {
        if (f.contentDocument) docs.push(f.contentDocument);
      } catch (e) {}
    }
    for (const doc of docs) {
      for (const el of doc.querySelectorAll("input, textarea, [contenteditable]")) {
        if (!secret(el) || !visible(el)) continue;
        globalThis.__browserdRestore.push([el, el.style.getPropertyValue("-webkit-text-security")]);
        el.style.setProperty("-webkit-text-security", "disc", "important");
        const b = el.getBoundingClientRect();
        const cover = doc.createElement("div");
        cover.style.cssText = "position:fixed;z-index:2147483647;pointer-events:none;background:#9e9e9e;" +
          "left:" + b.left + "px;top:" + b.top + "px;width:" + b.width + "px;height:" + b.height + "px";
        doc.documentElement.appendChild(cover);
        masks.push(cover);
        hidden.push(ref(el));
      }
    }
    return { masked: hidden };
  }

  // markHumanTyped remembers the focused field as the person's; when the focus is in a frame of
  // another site, it says which, and the daemon marks the field in that frame's world.
  function markHumanTyped() {
    const el = deepFocus();
    if (el && (tag(el) === "iframe" || tag(el) === "frame") && crossOrigin(el)) return { frame: frameRef(el) };
    if (el && (tag(el) === "input" || tag(el) === "textarea" || el.isContentEditable)) {
      humanTyped.add(el);
      return { marked: true };
    }
    return { marked: false };
  }

  // deepFocus is the element that has the focus, through same-origin frames and open shadow roots.
  function deepFocus() {
    let el = document.activeElement;
    for (;;) {
      if (el && (tag(el) === "iframe" || tag(el) === "frame")) {
        let inner = null;
        try {
          inner = el.contentDocument && el.contentDocument.activeElement;
        } catch (e) {}
        if (!inner) break;
        el = inner;
        continue;
      }
      if (el && el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement;
        continue;
      }
      break;
    }
    return el && el !== document.body && el !== document.documentElement ? el : null;
  }

  // focusedRef is the focused element's ref, or the frame of another site the focus is in.
  function focusedRef() {
    const el = deepFocus();
    if (!el) return { ref: "" };
    if ((tag(el) === "iframe" || tag(el) === "frame") && crossOrigin(el)) return { frame: frameRef(el) };
    return { ref: ref(el) };
  }

  // region is the outline the difference an action made is taken from: the whole page, as far as
  // 20 000 characters of it reach, since what an action changes is often far from its element (a
  // cart's count in the header, a message in a corner). No [covered by …]: an overlay that comes or
  // goes would change every line under it and bury what the action did.
  function region() {
    const o = outline(document.body || document.documentElement, 20000, { marks: false });
    return { lines: o.lines.map((l) => l.s), frames: o.frames.filter((f) => f.cross_origin) };
  }

  // route says where a ref lives: in this world's documents, or in a frame of another site (whose
  // ref it gives), or nowhere any more.
  function route(r) {
    if (lookup(r)) return { local: true };
    const f = frameByRef(r);
    if (f) return crossOrigin(f) ? { frame: r } : { local: true };
    if (typeof r === "string" && r.startsWith(prefix)) {
      const m = /^f\d+/.exec(r.slice(prefix.length));
      if (m) {
        const fr = frameByRef(prefix + m[0]);
        if (fr && crossOrigin(fr)) return { frame: prefix + m[0] };
      }
    }
    return { error: "stale", ref: r };
  }

  function frameElement(fref) {
    return frameByRef(fref);
  }

  function frameOrigin(fref) {
    const f = frameByRef(fref);
    if (!f) return { error: "stale", ref: fref };
    return contentOrigin(f);
  }

  // prepareFrame brings a frame into view, as prepare does an element.
  function prepareFrame(fref) {
    const f = frameByRef(fref);
    if (!f) return { error: "stale", ref: fref };
    const b = box(f);
    if (b.x < 0 || b.y < 0 || b.x + b.w > innerWidth || b.y + b.h > innerHeight) {
      f.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
    }
    return contentOrigin(f);
  }

  // hitFrame says whether a point lands on the frame, for an element inside it: a page can cover a
  // frame as it can an element.
  function hitFrame(fref, x, y) {
    const f = frameByRef(fref);
    if (!f) return { error: "stale", ref: fref };
    const t = topAt(x, y);
    if (t === f) return { ok: true };
    return { ok: false, covered_by: t ? coverer(t, f) : "" };
  }

  // crossFrames are the visible frames of another site this world's documents hold.
  function crossFrames() {
    const out = [];
    for (const f of allFrames(document)) {
      if (crossOrigin(f) && visible(f)) out.push(frameRef(f));
    }
    return out;
  }

  function setPrefix(p) {
    prefix = p;
    return true;
  }

  function markListened(els) {
    let n = 0;
    for (const el of els) {
      if (el && el.nodeType === Node.ELEMENT_NODE) {
        listened.add(el);
        n++;
      }
    }
    return n;
  }

  // listenerRoot is the document the daemon asks Chromium about, or null on a page too big to ask.
  function listenerRoot() {
    return document.getElementsByTagName("*").length > LISTENER_ELEMENTS ? null : document;
  }

  function hasText(text) {
    return (document.body ? document.body.innerText : "").includes(text);
  }

  function exists(r) {
    return !!lookup(r);
  }

  // A CAPTCHA is known by where its frame comes from: reCAPTCHA, hCaptcha, Cloudflare's Turnstile.
  function captchaSource(src) {
    let u;
    try {
      u = new URL(src, location.href);
    } catch (e) {
      return false;
    }
    const h = u.hostname;
    return /(^|\.)(hcaptcha\.com|recaptcha\.net)$/.test(h) || h === "challenges.cloudflare.com" ||
      (/(^|\.)google\.com$/.test(h) && u.pathname.startsWith("/recaptcha"));
  }

  function captchas() {
    const found = [];
    for (const f of allFrames(document)) {
      if (f.src && captchaSource(f.src) && visible(f)) found.push(f.src);
    }
    return found;
  }

  globalThis.__browserd = {
    snapshot, readable, prepare, measure, hit, hovered, focus, selectAll, selectOption, element, evidence, mask, markHumanTyped,
    hasText, exists, captchas, ref, describe, focusedRef, region, find, elementAt, scrollInfo, scrollBy, scrollToText,
    route, frameElement, frameOrigin, prepareFrame, hitFrame, crossFrames, setPrefix, markListened, listenerRoot,
    scrollState: () => {
      const st = scrollState();
      return { top: st.top, height: st.height, view: st.view, above: st.above, below: st.below, pane: st.el ? ref(st.el) : "" };
    },
    href: () => location.href,
  };
})();
