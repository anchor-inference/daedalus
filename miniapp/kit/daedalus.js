/*
 * The Daedalus page bridge: an optional convenience for a page shown inside the Daedalus app.
 *
 *   <script src="/pages/_kit/daedalus.js"></script>
 *   <script>Daedalus.ready().then(() => Daedalus.ui.toast("Hello"))</script>
 *
 * A page is served on the app's own origin behind the app's sign-in, so it can already call any
 * /api/ route with fetch; nothing here is required. What the bridge adds is what only the app knows:
 * the live theme (the app paints its themes as inline values on its own root, which no stylesheet of
 * the page can read), the app's toasts and navigation, and events the app forwards to the page.
 *
 * Outside the app (the page opened in a tab of its own) every call still works: the theme stays as
 * the page's stylesheet has it, a toast is logged, and navigation goes to the app in this tab.
 *
 * A page may have an agent of its own, the session bound to it: Daedalus.agent.ask sends it a message
 * marked as the page's, and Daedalus.agent.on hears its replies and runs, which the app forwards
 * while the page is open in it. What the agent pushes with PageEmit arrives as Daedalus.on(event).
 *
 * The API is versioned by Daedalus.version (semver). A minor version adds; only a major one removes.
 */
(function () {
  "use strict";
  if (window.Daedalus && window.Daedalus.version) return;

  var VERSION = "1.1.0";
  var PROTOCOL = 1;
  var parentWindow = window.parent !== window ? window.parent : null;
  var origin = window.location.origin;
  var pending = new Map();
  var listeners = new Map();
  var nextId = 1;
  var context = { theme: null, scheme: null, lang: document.documentElement.lang || "en", phone: false, slug: slugFromPath(), embedded: !!parentWindow };
  var readyPromise = null;

  function slugFromPath() {
    var match = /^\/pages\/([^/]+)/.exec(window.location.pathname);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function emit(event, data) {
    var set = listeners.get(event);
    if (!set) return;
    set.forEach(function (fn) {
      try {
        fn(data);
      } catch (error) {
        console.error("Daedalus: a listener for " + event + " failed", error);
      }
    });
  }

  function applyTheme(data) {
    if (!data || typeof data !== "object") return;
    var root = document.documentElement;
    var tokens = data.tokens || {};
    Object.keys(tokens).forEach(function (name) {
      if (name.indexOf("--") === 0) root.style.setProperty(name, tokens[name]);
    });
    if (data.scheme) root.dataset.scheme = data.scheme;
    if (data.theme) root.dataset.theme = data.theme;
    if (data.lang) root.lang = data.lang;
    root.dataset.phone = data.phone ? "1" : "0";
    context.theme = data.theme || null;
    context.scheme = data.scheme || null;
    context.lang = data.lang || context.lang;
    context.phone = !!data.phone;
  }

  function call(method, params) {
    if (!parentWindow) return Promise.reject(new Error("not inside the Daedalus app"));
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      pending.set(id, { resolve: resolve, reject: reject });
      parentWindow.postMessage({ daedalus: PROTOCOL, id: id, method: method, params: params || {} }, origin);
      setTimeout(function () {
        if (pending.has(id)) {
          pending.delete(id);
          reject(new Error("the app did not answer " + method));
        }
      }, 10000);
    });
  }

  window.addEventListener("message", function (e) {
    // Only the app that frames this page speaks the protocol, and it is always this origin.
    if (e.source !== parentWindow || e.origin !== origin) return;
    var data = e.data;
    if (!data || data.daedalus !== PROTOCOL) return;
    if (typeof data.id === "number" && pending.has(data.id)) {
      var waiting = pending.get(data.id);
      pending.delete(data.id);
      if (data.error) waiting.reject(new Error(String(data.error)));
      else waiting.resolve(data.result);
      return;
    }
    if (typeof data.event === "string") {
      if (data.event === "theme") applyTheme(data.data);
      emit(data.event, data.data);
    }
  });

  function ready() {
    if (readyPromise) return readyPromise;
    if (!parentWindow) {
      readyPromise = Promise.resolve(context);
      return readyPromise;
    }
    readyPromise = call("ready", { version: VERSION })
      .then(function (result) {
        if (result && result.theme) applyTheme(result.theme);
        return context;
      })
      .catch(function () {
        return context;
      });
    return readyPromise;
  }

  function apiFetch(path, init) {
    var options = Object.assign({ credentials: "same-origin" }, init || {});
    var headers = new Headers(options.headers || {});
    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData) && !(options.body instanceof Blob) && !(options.body instanceof ArrayBuffer) && !(options.body instanceof URLSearchParams) && !ArrayBuffer.isView(options.body)) {
      options.body = JSON.stringify(options.body);
      if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    }
    options.headers = headers;
    var url = path.indexOf("/") === 0 ? path : "/api/" + path;
    return fetch(url, options).then(function (response) {
      if (!response.ok) {
        return response.text().then(function (text) {
          var detail = text;
          try {
            detail = JSON.parse(text).detail || text;
          } catch (ignored) {
            /* plain text */
          }
          var error = new Error(typeof detail === "string" ? detail : response.statusText);
          error.status = response.status;
          throw error;
        });
      }
      var type = response.headers.get("Content-Type") || "";
      return type.indexOf("application/json") === 0 ? response.json() : response.text();
    });
  }

  function pagePath(rest) {
    if (!context.slug) throw new Error("this needs the page's address (/pages/<slug>/)");
    return "/api/pages/" + encodeURIComponent(context.slug) + (rest ? "/" + rest : "");
  }

  function storagePath(key) {
    var base = pagePath("storage");
    return key === undefined ? base : base + "/" + String(key).split("/").map(encodeURIComponent).join("/");
  }

  var storage = {
    get: function (key) {
      return apiFetch(storagePath(key)).then(
        function (found) {
          return found.value;
        },
        function (error) {
          if (error.status === 404) return null;
          throw error;
        }
      );
    },
    set: function (key, value) {
      return apiFetch(storagePath(key), { method: "PUT", body: { value: value } });
    },
    delete: function (key) {
      return apiFetch(storagePath(key), { method: "DELETE" }).then(
        function () {
          return true;
        },
        function (error) {
          if (error.status === 404) return false;
          throw error;
        }
      );
    },
    list: function (prefix) {
      return apiFetch(storagePath() + (prefix ? "?prefix=" + encodeURIComponent(prefix) : "")).then(function (found) {
        return found.keys;
      });
    },
  };

  function inApp(path) {
    return path.indexOf("/app") === 0 ? path : "/app" + (path.indexOf("/") === 0 ? "" : "/") + path;
  }

  var ui = {
    toast: function (text) {
      if (!parentWindow) {
        console.info("Daedalus:", text);
        return Promise.resolve(false);
      }
      return call("toast", { text: String(text) });
    },
    navigate: function (path) {
      if (!parentWindow) {
        window.location.href = inApp(path);
        return Promise.resolve(true);
      }
      return call("navigate", { path: String(path) });
    },
    openSession: function (id) {
      return ui.navigate("/app/agents/" + encodeURIComponent(id));
    },
    openFile: function (sessionId, path) {
      if (!parentWindow) {
        window.location.href = "/app/agents/" + encodeURIComponent(sessionId) + "?panel=preview&path=" + encodeURIComponent(path);
        return Promise.resolve(true);
      }
      return call("openFile", { sessionId: String(sessionId), path: String(path) });
    },
  };

  // The agent's conversation, as the app forwards it: its answers, its run's state, and every event
  // of its session for a page that wants the whole of it.
  var AGENT_EVENTS = { message: "agent.message", run: "agent.run", event: "agent.event" };

  var agent = {
    ask: function (text, options) {
      var o = options || {};
      var form = new FormData();
      form.append("text", text == null ? "" : String(text));
      if (o.silent) form.append("silent", "1");
      if (o.followUp) form.append("follow_up", "1");
      (o.attachments || []).forEach(function (file, i) {
        if (file instanceof Blob) form.append("files", file, file.name || "file-" + (i + 1));
      });
      return apiFetch(pagePath("ask"), { method: "POST", body: form });
    },
    on: function (kind, fn) {
      var name = AGENT_EVENTS[kind];
      if (!name) throw new Error("agent.on takes message, run or event, not " + kind);
      return on(name, fn);
    },
    open: function () {
      if (!parentWindow) {
        window.location.href = "/app/p/" + encodeURIComponent(context.slug || "");
        return Promise.resolve(false);
      }
      return call("agent.open", {});
    },
    session: function () {
      return apiFetch(pagePath()).then(function (page) {
        return page.agent || null;
      });
    },
  };

  function on(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return function () {
      listeners.get(event).delete(fn);
    };
  }

  window.Daedalus = Object.freeze({
    version: VERSION,
    protocol: PROTOCOL,
    ready: ready,
    get context() {
      return Object.assign({}, context);
    },
    theme: Object.freeze({
      get current() {
        return { theme: context.theme, scheme: context.scheme, phone: context.phone, lang: context.lang };
      },
      onChange: function (fn) {
        return on("theme", fn);
      },
    }),
    ui: Object.freeze(ui),
    storage: Object.freeze(storage),
    agent: Object.freeze(agent),
    api: apiFetch,
    on: on,
  });
  // A page that never calls ready() still gets the app's look.
  ready();
})();
