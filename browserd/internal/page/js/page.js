// The daemon's script, run in an isolated world of each document: the page's own scripts cannot see
// it, reach its ref table, or change what it reads. It is evaluated once per world and defines one
// global, __browserd, whose functions the daemon calls with JSON arguments and whose answers come
// back by value.
(() => {
  if (globalThis.__browserd) return;

  const refOf = new WeakMap();      // element -> ref
  const byRef = new Map();          // ref -> WeakRef(element)
  const frameOf = new WeakMap();    // iframe element -> frame number
  const humanTyped = new WeakSet(); // fields a person typed into while driving
  let nextRef = 1;
  let nextFrame = 1;

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
    // The frame an element lives in: "" for the top document, "f<k>" inside an iframe.
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

  function ref(el) {
    let r = refOf.get(el);
    if (!r) {
      r = ownerRefPrefix(el) + "e" + nextRef++;
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
    const m = /^f(\d+)$/.exec(r);
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

  function tag(el) {
    return el.localName || "";
  }

  function inputType(el) {
    return (el.getAttribute("type") || "text").toLowerCase();
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
    let out = "";
    const kids = el.shadowRoot ? el.shadowRoot.childNodes : el.childNodes;
    for (const c of kids) out += " " + textOf(c, depth + 1);
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

  function accessibleName(el, r) {
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
      if (!n && t === "input" && it === "file") n = "Choose file";
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
    if (NAME_FROM_CONTENT.has(r || role(el))) {
      n = textOf(el, 0);
      if (collapse(n)) return clip(n, 120);
    }
    return clip(el.getAttribute("title") || "", 120);
  }

  function visible(el) {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return false;
    if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true });
    const s = el.ownerDocument.defaultView.getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden";
  }

  function interactive(el, r) {
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

  // outline walks the tree from root and returns its lines and the frames it read.
  function outline(root, maxChars) {
    const lines = [];
    const frames = [];
    let used = 0;
    let truncated = false;
    let refs = 0;
    function emit(depth, line) {
      if (truncated) return;
      const s = "  ".repeat(depth) + "- " + line;
      if (used + s.length + 1 > maxChars) {
        truncated = true;
        return;
      }
      used += s.length + 1;
      lines.push(s);
    }
    function walk(node, depth) {
      if (truncated || depth > 60) return;
      if (node.nodeType === Node.TEXT_NODE) return;
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const t = tag(el);
      if (t === "script" || t === "style" || t === "noscript" || t === "template" || t === "head") return;
      if (!visible(el)) return;
      const r = role(el);
      const isInteractive = interactive(el, r);
      const significant = isInteractive || LANDMARKS.has(r) || STRUCTURE.has(r) || r === "textbox";
      let childDepth = depth;
      if (significant) {
        const name = accessibleName(el, r);
        let line = (r || (isInteractive ? "generic" : t)) + (name ? " " + quote(name) : "");
        if (isInteractive || r === "iframe") {
          line += " [ref=" + (r === "iframe" ? "f" + frameNumber(el) : ref(el)) + "]";
          refs++;
        }
        const st = states(el, r);
        if (st.length) line += " " + st.join(" ");
        const v = valueOf(el, r);
        if (v !== null && v !== "") line += " value=" + quote(v);
        if (r === "link" && el.href) line += " url=" + quote(clip(el.href, 200));
        if (r === "iframe") {
          let cross = false;
          try {
            cross = !el.contentDocument;
          } catch (e) {
            cross = true;
          }
          frames.push({ ref: "f" + frameNumber(el), url: el.src || "", cross_origin: cross });
          if (cross) line += " (another site's frame; its content is not read)";
        }
        emit(depth, line);
        childDepth = depth + 1;
        // A named control's own text is its name: not repeated underneath it.
        if (isInteractive && r !== "iframe" && NAME_FROM_CONTENT.has(r)) return;
        if (r === "heading" || r === "img") return;
      }
      // A field's content is its value, which the line above shows or, for a secret, withholds.
      if (t === "textarea" || t === "input" || t === "select") return;
      let text = "";
      // A label's words are its control's name, already on the control's line.
      const labelsAControl = t === "label" && !!el.control;
      const flush = () => {
        const s = collapse(text);
        if (s && !labelsAControl) emit(childDepth, "text " + quote(clip(s, 300)));
        text = "";
      };
      for (const c of childrenOf(el)) {
        if (c.nodeType === Node.TEXT_NODE) {
          text += " " + c.data;
          continue;
        }
        if (c.nodeType === Node.ELEMENT_NODE && isInline(c) && !significantish(c)) {
          text += " " + textOf(c, 0);
          continue;
        }
        flush();
        walk(c, childDepth);
      }
      flush();
    }
    walk(root, 0);
    return { lines, frames, truncated, refs };
  }

  const INLINE = new Set(["span", "b", "i", "em", "strong", "small", "code", "abbr", "cite", "q", "sub", "sup",
    "time", "mark", "u", "s", "kbd", "var", "br", "wbr", "bdi", "bdo", "data", "font"]);

  function isInline(el) {
    return INLINE.has(tag(el));
  }

  function significantish(el) {
    const r = role(el);
    return interactive(el, r) || LANDMARKS.has(r) || STRUCTURE.has(r);
  }

  function snapshot(scope, maxChars) {
    let root = document.body || document.documentElement;
    if (scope) {
      root = lookup(scope) || frameByRef(scope);
      if (!root) return { error: "stale", ref: scope };
    }
    const o = outline(root, maxChars);
    let text = o.lines.join("\n");
    if (o.truncated) {
      const focus = document.activeElement && document.activeElement !== document.body ? refOf.get(document.activeElement) : "";
      text += "\n- [the page is longer than this; take a snapshot with scope=<ref> of a region" +
        (focus ? ", such as the focused " + focus : "") + "]";
    }
    return { url: location.href, title: document.title, text, refs: o.refs, truncated: o.truncated, frames: o.frames };
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

  // offset is where an element's document sits in the top viewport.
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

  function describe(el) {
    const r = role(el);
    const form = el.form || (el.closest && el.closest("form"));
    const out = { role: r || tag(el), name: accessibleName(el, r), tag: tag(el) };
    if (tag(el) === "input") out.type = inputType(el);
    const ac = el.getAttribute("autocomplete");
    if (ac) out.autocomplete = ac;
    if (el.href) out.href = el.href;
    if (form) out.form_action = form.action || location.href;
    out.secret = secret(el);
    out.secret_kind = secretKind(el);
    out.disabled = !!(el.disabled || el.getAttribute("aria-disabled") === "true");
    out.checked = el.checked === true || el.getAttribute("aria-checked") === "true";
    out.file = tag(el) === "input" && inputType(el) === "file";
    out.select = tag(el) === "select";
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

  // hit says whether the point lands on the element (or inside it), not on something covering it.
  function hit(r, x, y) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    let doc = document;
    let px = x, py = y;
    let target = doc.elementFromPoint(px, py);
    // Into same-origin frames.
    while (target && (tag(target) === "iframe" || tag(target) === "frame")) {
      let inner;
      try {
        inner = target.contentDocument;
      } catch (e) {
        inner = null;
      }
      if (!inner) break;
      const o = box(target);
      px -= o.x; py -= o.y;
      target = inner.elementFromPoint(px, py);
    }
    while (target && target.shadowRoot && target.shadowRoot.elementFromPoint) {
      const inner = target.shadowRoot.elementFromPoint(px, py);
      if (!inner || inner === target) break;
      target = inner;
    }
    let ok = false;
    for (let n = target; n; n = n.parentNode || n.host) {
      if (n === el) {
        ok = true;
        break;
      }
    }
    return { ok, covered_by: ok || !target ? "" : describe(target).role + " " + quote(describe(target).name) };
  }

  function focus(r) {
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    el.focus({ preventScroll: true });
    return { focused: el.ownerDocument.activeElement === el };
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

  function element(r) {
    return lookup(r);
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
    const el = lookup(r);
    if (!el) return { error: "stale", ref: r };
    const d = describe(el);
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
      name: d.name, role: d.role, tag: d.tag, type: d.type || "", text,
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

  function markHumanTyped() {
    const el = deepFocus();
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

  // selection is what the operator copies while they drive: the text selected in the focused field,
  // or else in the document that holds the focus (through same-origin frames and open shadow roots),
  // at most max characters. A password field's value is withheld even from the operator: the copy
  // lands in a clipboard other programs read, and a password that leaves the page that way is one
  // nobody meant to move. A field only the operator typed into is theirs to copy, so it is not
  // withheld the way it is from the agent.
  function selection(max) {
    let doc = document;
    let el = doc.activeElement;
    let root = null;
    for (;;) {
      if (el && (tag(el) === "iframe" || tag(el) === "frame")) {
        let inner = null;
        try {
          inner = el.contentDocument;
        } catch (e) {}
        if (!inner) break;
        doc = inner;
        el = inner.activeElement;
        root = null;
        continue;
      }
      if (el && el.shadowRoot && el.shadowRoot.activeElement) {
        root = el.shadowRoot;
        el = root.activeElement;
        continue;
      }
      break;
    }
    let text = "";
    if (el && (tag(el) === "input" || tag(el) === "textarea")) {
      const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
      if ((tag(el) === "input" && inputType(el) === "password") || /password/.test(ac)) {
        return { text: "", truncated: false, withheld: true };
      }
      let start = null, end = null;
      try {
        start = el.selectionStart;
        end = el.selectionEnd;
      } catch (e) {}
      // An email or number field has no selection offsets to read; the document's selection then
      // holds what Chromium shows selected in it.
      if (start !== null && end !== null) text = String(el.value || "").slice(start, end);
    }
    if (!text) {
      const sel = root && typeof root.getSelection === "function" ? root.getSelection() : doc.getSelection();
      text = sel ? sel.toString() : "";
    }
    const truncated = text.length > max;
    if (truncated) {
      // Never between the halves of a surrogate pair: half a character is not text.
      let cut = max;
      const c = text.charCodeAt(cut - 1);
      if (c >= 0xd800 && c <= 0xdbff) cut--;
      text = text.slice(0, cut);
    }
    return { text, truncated, withheld: false };
  }

  function focusedRef() {
    const el = deepFocus();
    return el ? ref(el) : "";
  }

  // region is the outline the difference an action made is taken from: the whole page, as far as
  // 20 000 characters of it reach, since what an action changes is often far from its element (a
  // cart's count in the header, a message in a corner).
  function region() {
    return outline(document.body || document.documentElement, 20000).lines;
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
    snapshot, readable, prepare, hit, focus, selectAll, selectOption, element, evidence, mask, markHumanTyped, selection,
    hasText, exists, captchas, ref, describe, focusedRef, region, href: () => location.href,
  };
})();
