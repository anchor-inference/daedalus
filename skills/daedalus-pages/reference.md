# Kit and bridge reference

Both are optional. A page that uses neither works just as well; it only does not look like the app or
follow its theme by itself.

## The kit: `/pages/_kit/kit.css`

The file starts with the app's own design tokens, copied from the app on every build, then a small
set of components. Every class starts with `dk-`, so nothing collides with a page's own styles or a
framework. The classes are added to, never renamed or removed.

| Class | |
|---|---|
| `dk-page` | the page's padded body |
| `dk-stack`, `dk-row-flex`, `dk-grid` | a column, a wrapping row, a responsive grid of cards |
| `dk-btn` (`.primary`, `.ghost`, `.danger`, `.small`, `.icon`) | a button (also on `<a>` and `<label>`) |
| `dk-field` with a `<label>`; `dk-input`, `dk-select`, `dk-textarea`; `dk-error` | a labelled field; `aria-invalid="true"` marks a bad one |
| `dk-card` (`.flat`) | a raised surface |
| `dk-list` of `dk-row` (`dk-row-main`, `dk-row-title`, `dk-row-meta`; `.pressable`, `aria-selected`) | a list |
| `dk-tabs` of `dk-tab` (`aria-selected="true"` on the open one) | tabs |
| `dk-toolbar` with `dk-spacer` | a sticky bar of actions at the top |
| `dk-badge` (`.ok`, `.warn`, `.bad`, `.info`, `.accent`) | a count or a state |
| `dk-empty` | an empty state: a `<b>` line and a sentence |
| `dk-split` of two `dk-pane` (`--dk-side` sets the side's width) | side column and main area; stacked on a phone |
| `dk-h1`, `dk-h2`, `dk-section-title`, `dk-muted`, `dk-meta`, `dk-mono` | text |

Tokens, usable in any style of the page:

- colours: `--bg`, `--surface-1`, `--surface-2`, `--surface-3`, `--fg`, `--fg-2`, `--fg-3`, `--line`,
  `--line-strong`, `--accent`, `--accent-ink`, `--primary`, `--primary-ink`, `--ok`, `--warn`, `--bad`, `--info`
- type: `--font`, `--mono`, `--fs-11` … `--fs-22`, `--lh-ui`, `--lh-prose`
- space and shape: `--space-1` … `--space-8`, `--page-pad`, `--gutter`, `--radius-sm`, `--radius-md`,
  `--radius-pill`
- sizes: `--ctl-h`, `--ctl-h-sm`, `--row-h`, `--head-h` (they grow to touch sizes below 1024 px)

## The bridge: `/pages/_kit/daedalus.js`

| | |
|---|---|
| `Daedalus.version` | semver; a minor version adds, only a major one removes |
| `Daedalus.ready()` | resolves with `{theme, scheme, lang, phone, slug, embedded}` once the app answered (at once outside the app) |
| `Daedalus.context` | the same, now |
| `Daedalus.theme.current`, `Daedalus.theme.onChange(fn)` | the theme; `fn` gets `{tokens, scheme, theme, lang, phone}` on every change |
| `Daedalus.ui.toast(text)` | a toast in the app |
| `Daedalus.ui.navigate(path)` | go to an address inside the app (`/app/…`) |
| `Daedalus.ui.openSession(id)`, `Daedalus.ui.openFile(sessionId, path)` | open a session, or a workspace file in its Preview |
| `Daedalus.storage.get(key)`, `.set(key, value)`, `.delete(key)`, `.list(prefix)` | the page's durable values (any JSON, keys may hold `/`) |
| `Daedalus.api(path, init)` | `fetch` for `/api/…` (or the part after `/api/`); a plain object body is sent as JSON; resolves with the parsed answer, rejects with an error that has `status` |
| `Daedalus.on(event, fn)` | events the app forwards; returns the function that stops listening. Whatever you push with `PageEmit(slug, event, data)` arrives here under `event` |
| `Daedalus.agent.ask(text, {attachments, silent, followUp})` | a message to the page's agent (you, when this session is bound to the page); resolves `{run_id, session_id}`, rejects with `status` 409 when the page has no agent. `silent`: the page reporting on its own, not the operator's request |
| `Daedalus.agent.on("message" \| "run" \| "event", fn)` | the agent's answers (`{text, run_id, seq, at}`, once its run ends), its run state (`{run_id, status}`), every event of its session; inside the app only |
| `Daedalus.agent.open()`, `Daedalus.agent.session()` | show the agent's conversation beside the page; the bound session `{id, title, running, gone}` or null |

Opened in a tab of its own, every call still works: the theme stays as the stylesheet has it, a toast
is logged to the console, navigation goes to the app in that tab.

## Snippets

A canvas or chart that follows the theme:

```js
function paint() {
  const style = getComputedStyle(document.documentElement);
  chart.options.color = style.getPropertyValue("--fg").trim();
  chart.options.backgroundColor = style.getPropertyValue("--surface-1").trim();
  chart.update();
}
Daedalus.ready().then(paint);
Daedalus.theme.onChange(paint);
```

The page's words in the operator's language:

```js
const STRINGS = { en: { save: "Save" }, ru: { save: "Сохранить" } };
let lang = "en";
const t = (key) => (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.en[key] || key;
const translate = () => document.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
Daedalus.ready().then((c) => { lang = c.lang in STRINGS ? c.lang : "en"; translate(); });
Daedalus.theme.onChange((theme) => { if (theme.lang in STRINGS && theme.lang !== lang) { lang = theme.lang; translate(); } });
```

A value kept across devices:

```js
const saved = await Daedalus.storage.get("filters");      // null when nothing is stored
await Daedalus.storage.set("filters", { status: "open" });
```

Any API route, with the operator's session:

```js
const page = await Daedalus.api("pages/orders");          // GET /api/pages/orders
await Daedalus.api("sessions/" + id + "/messages", { method: "POST", body: { text: "Refresh the report", follow_up: true } });
```

A page and its agent (the `split` template is this, complete):

```js
// The page: a button asks the agent, its run and answer come back, and what it pushes is applied.
optimise.onclick = () => Daedalus.agent.ask("Optimise this mesh", { attachments: [meshFile] });
Daedalus.agent.on("run", (run) => (spinner.hidden = run.status !== "running"));
Daedalus.agent.on("message", (reply) => log.append(reply.text));
Daedalus.on("scene.updated", async () => viewer.load(await Daedalus.storage.get("scene")));
```

```
# The agent, answering "(Sent from the page «CAD» …) Optimise this mesh":
PageStorage("cad", "set", "scene", {"file": "model.glb", "triangles": 120000})
PageEmit("cad", "scene.updated", {"file": "model.glb"})
```

A React or Vue app built with Vite, imported as a page:

```js
// vite.config.js
export default { base: "/pages/orders/" };
```

```
npm run build            # in the workspace
PageImport(slug="orders", source_path="dist", replace=true)
```
