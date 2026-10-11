# Custom pages

A custom page is a screen of your own inside the Daedalus app: a dashboard, a form, a specialised
client for some API, a 3D or CAD viewer, a calculator, or an existing web application you already
run. It opens at `/app/p/<slug>`, can sit in the rail and the phone drawer, follows your theme, and
works on a phone and a desktop.

A page is **data, not app code**. It is a folder in the state volume, so adding, changing or removing
one never rebuilds the app, restarts anything or interrupts a running session. A broken page breaks
only its own frame, and the previous version is one click away.

## No limits on what a page is

Pages are yours. Daedalus places no restriction on what goes into one:

- **Any files, any type.** HTML, JavaScript modules, WebAssembly, fonts, images, video, 3D models,
  data files, archives. Each is served with its content type (`.wasm` as `application/wasm`, `.glb`
  as `model/gltf-binary`, …); a type nobody knows is served as `application/octet-stream`.
- **Any framework or library**, loaded from the page's own files or from any CDN. External scripts,
  styles, fonts, `fetch` to other sites, WebSockets, workers, service workers scoped to the page, and
  WebAssembly all work. The only policy a page is given is `frame-ancestors 'self'`, so that the app
  can frame it.
- **The whole API.** A page is served on the app's own origin, behind the app's sign-in, in a frame
  that is not sandboxed. Its own `fetch('/api/…')` carries your session cookie, exactly like the app.
  Treat a page like any program you run as yourself.
- **Any web application**, through the `service` kind: a server an agent runs (or you run) on this
  machine is reverse-proxied at the page's address, any method and path, request bodies of any size
  streamed through, responses streamed back (server-sent events, long downloads), WebSockets relayed.
- **Size caps are a safety net, not a design limit.** By default a page's draft may hold 2 GB with no
  limit on the number of files; set any cap to `0` to lift it (see [the settings](#settings)).

The [kit](#the-kit) and the [bridge](#the-bridge) are optional conveniences for looking native and
talking to the app. A page that uses neither works just as well.

## Kinds

| Kind | What `/pages/<slug>/` serves |
|---|---|
| `static` | the files of the published version; an address with no extension that names no file is the entry, so an app with its own client-side router survives a reload on any of its addresses |
| `service` | a reverse proxy to `http://127.0.0.1:<port>/`, the target being a port or the name of a service an agent started; the server sees `X-Forwarded-Prefix: /pages/<slug>` |
| `link` | another site in a full-size frame, with a link to open it in a tab of its own when the site refuses to be framed |

For a `service` page Daedalus drops `X-Frame-Options` from the answers, turns a `frame-ancestors`
directive into `'self'`, moves a cookie the server sets on `/` under `/pages/<slug>/`, and keeps a
redirect to the server's root under the page's prefix. The app's own session cookie is never
forwarded to the server.

## On disk

```
<state>/pages/<slug>/
  page.json          the manifest
  draft/             the files being worked on
  versions/<n>/      each published version, exactly as published
```

A slug is 1 to 63 lower-case letters, digits and hyphens, starting with a letter or a digit. A file's
path inside a page is relative and may not leave the page's folder.

Publishing copies the draft into a new version directory and switches the manifest to it in one
rename, so a reader sees the old version or the new one and never half of either. Rolling back
switches the live version to any kept one; nothing is deleted. The newest `keep_versions` versions
are kept, and the live one always is. Removing a page deletes its folder and its stored values.

### The manifest

```json
{
  "slug": "orders",
  "title": {"en": "Orders", "ru": "Заказы"},
  "icon": "board",
  "icon_path": null,
  "session_id": null,
  "created_at": "2026-10-11T09:00:00Z",
  "updated_at": "2026-10-11T09:30:00Z",
  "version": 2,
  "versions": [
    {"n": 1, "at": "…", "note": "", "kind": "static", "entry": "index.html", "target": null, "files": 3, "bytes": 18211},
    {"n": 2, "at": "…", "note": "Prices for October", "kind": "static", "entry": "index.html", "target": null, "files": 3, "bytes": 18530}
  ],
  "draft": {"kind": "static", "entry": "index.html", "target": null, "updated_at": "…"}
}
```

- `title` is a string, or one per language.
- `icon` is the name of one of the app's icons; `icon_path` is a single SVG path (`d`) on a 24-unit
  grid, stroked in the current colour like the app's own icons. Either may be null.
- `session_id` names the agent session that owns the page, if any.
- `version` is the live version, null until the first publish.
- The title, icon and session apply at once. The kind, entry and target are the draft's and go live
  with the next publish, so changing a service page's port can be rolled back like any other change.

## The API

Every route is behind the normal sign-in (the session cookie, the API token in `X-Daedalus-Token`, or
Telegram's init data).

| Route | |
|---|---|
| `GET /api/pages` | the published pages, for the navigation: `{"pages": [{"slug", "title", "titles", "icon", "icon_path", "path", "version", "updated_at"}]}`; `title` is the plain or English title, `titles` one per language, `path` is `/app/p/<slug>`. `?all=1` adds pages with nothing published (`version` null) |
| `POST /api/pages` | create: `{"slug", "title", "kind", "entry", "target", "icon", "icon_path", "session_id"}` |
| `GET /api/pages/<slug>` | the manifest with `url` (`/pages/<slug>/`), `draft_url`, and the live `kind`, `entry`, `target`; `versions` newest first |
| `PATCH /api/pages/<slug>` | change any of `title`, `icon`, `icon_path`, `session_id`, `kind`, `entry`, `target` |
| `DELETE /api/pages/<slug>` | remove the page, its versions and its storage |
| `PUT /api/pages/<slug>/draft/<path>` | write one draft file; the body is the file, as it is, streamed to disk |
| `GET`, `DELETE /api/pages/<slug>/draft/<path>` | read or delete one draft file (deleting a folder's path deletes the folder) |
| `GET /api/pages/<slug>/files` | the draft's files; `?version=<n>` for a published version's |
| `POST /api/pages/<slug>/publish` | the draft becomes a new live version; `{"note": "…"}` is optional |
| `POST /api/pages/<slug>/rollback` | `{"version": <n>}` becomes the live version |
| `GET /api/pages/<slug>/storage` | the page's stored keys, `?prefix=` to narrow: `{"keys": [{"key", "bytes", "updated_at"}]}` |
| `GET`, `PUT`, `DELETE /api/pages/<slug>/storage/<key>` | one stored value; `PUT` takes `{"value": <any JSON>}`; a key may contain `/` |
| `POST /api/pages/_signin` | gives a browser signed in by token or Telegram the session cookie, so the app's frame can open a page |

Every change publishes a `pages.changed` event on `/api/events` (`{"slug", "change", "version"}`,
`change` being `created`, `updated`, `draft`, `published`, `rolled_back` or `removed`), so an open app
redraws its navigation and an open page without polling.

### Trying a draft

`/pages/<slug>/?draft=1` serves the draft instead of the live version. The page's own scripts,
styles and fetches are served from the draft too (a cookie scoped to the page carries the choice);
opening the page again without `?draft=1` returns to the live version. The app's version menu offers
the same as "Show the draft".

## The kit

```html
<link rel="stylesheet" href="/pages/_kit/kit.css">
```

`kit.css` starts with the app's own design tokens, copied from the app on every build so they never
fall behind, then adds a small set of components. Every class starts with `dk-`, so the kit never
collides with a page's own styles or a framework; the classes are added to, never renamed or removed.

| Class | |
|---|---|
| `dk-page` | the page's padded body |
| `dk-stack`, `dk-row-flex`, `dk-grid` | a column, a wrapping row, a responsive grid |
| `dk-btn` (`.primary`, `.ghost`, `.danger`, `.small`, `.icon`) | a button |
| `dk-field` with `dk-label`; `dk-input`, `dk-select`, `dk-textarea`; `dk-error` | a labelled field |
| `dk-card` (`.flat`) | a raised surface |
| `dk-list` of `dk-row` (`dk-row-main`, `dk-row-title`, `dk-row-meta`; `.pressable`, `aria-selected`) | a list |
| `dk-tabs` of `dk-tab` (`aria-selected="true"` on the open one) | tabs |
| `dk-toolbar` with `dk-spacer` | a sticky bar of actions |
| `dk-badge` (`.ok`, `.warn`, `.bad`, `.info`, `.accent`) | a count or a state |
| `dk-empty` | an empty state |
| `dk-split` of two `dk-pane` (`--dk-side` sets the side's width) | a side column and the main area; stacked on a phone |
| `dk-h1`, `dk-h2`, `dk-section-title`, `dk-muted`, `dk-meta`, `dk-mono` | text |

The tokens are usable directly: `var(--bg)`, `var(--surface-1)`, `var(--fg)`, `var(--fg-2)`,
`var(--accent)`, `var(--line)`, `var(--ok)`, `var(--bad)`, `var(--font)`, `var(--mono)`, the type steps
`var(--fs-12)` … `var(--fs-22)`, the spacing `var(--space-1)` … `var(--space-8)`, the radii, and the
row and control sizes. Below 1024 px they switch to the phone's sizes, the same as the app.

## The bridge

```html
<script src="/pages/_kit/daedalus.js"></script>
```

| | |
|---|---|
| `Daedalus.version` | the bridge's version (semver); a minor version adds, only a major one removes |
| `Daedalus.ready()` | resolves with the context (`theme`, `scheme`, `lang`, `phone`, `slug`, `embedded`) once the app has answered; outside the app it resolves at once |
| `Daedalus.theme.current`, `Daedalus.theme.onChange(fn)` | the app's theme. The app sends the computed value of every token whenever its look changes (a theme picked in Appearance, the system turning dark, the window crossing the phone width); the bridge writes them onto the page's root and sets `data-scheme`, `data-theme` and `lang` there |
| `Daedalus.ui.toast(text)` | a toast in the app |
| `Daedalus.ui.navigate(path)` | go to an address inside the app (`/app/…`) |
| `Daedalus.ui.openSession(id)` | open an agent session |
| `Daedalus.ui.openFile(sessionId, path)` | open a file of a session's workspace in its Preview |
| `Daedalus.storage.get(key)`, `.set(key, value)`, `.delete(key)`, `.list(prefix)` | the page's own durable values, shared across your devices (any JSON) |
| `Daedalus.api(path, init)` | `fetch` for an API route with the session cookie: `path` is `/api/…` or the part after `/api/`; a plain object body is sent as JSON; resolves with the parsed answer, rejects with an error carrying `status` |
| `Daedalus.on(event, fn)` | events the app forwards to the page; returns the function that stops listening. `theme` is one; `agent.message` is reserved for a page's own agent |

The bridge speaks `postMessage` with the app that frames the page and checks that every message comes
from that frame. Opened in a tab of its own, a page keeps working: the theme stays as its stylesheet
has it, a toast is written to the console, and navigation goes to the app in that tab.

## A minimal page

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/pages/_kit/kit.css">
  <script src="/pages/_kit/daedalus.js"></script>
</head>
<body>
  <main class="dk-page dk-stack">
    <div class="dk-card dk-row-flex">
      <input class="dk-input" id="note" placeholder="A note">
      <button class="dk-btn primary" id="save">Save</button>
    </div>
  </main>
  <script>
    Daedalus.storage.get("note").then((v) => { if (v) note.value = v; });
    save.onclick = async () => {
      await Daedalus.storage.set("note", note.value);
      Daedalus.ui.toast("Saved");
    };
  </script>
</body>
</html>
```

```bash
curl -X POST http://<host>:<port>/api/pages -H "X-Daedalus-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug": "notes", "title": "Notes", "icon": "pen"}'
curl -X PUT http://<host>:<port>/api/pages/notes/draft/index.html -H "X-Daedalus-Token: $TOKEN" --data-binary @index.html
curl -X POST http://<host>:<port>/api/pages/notes/publish -H "X-Daedalus-Token: $TOKEN"
```

The page is then at `/app/p/notes`.

## Built by an agent

Ask for a page in a chat and the agent builds it with the `pages` tools, guided by the bundled
`daedalus-pages` skill:

| Tool | |
|---|---|
| `PageCreate` | a new page (a draft) from a template: `blank`, `dashboard`, `split` (a tool with a panel for the page's agent), `viewer` (three.js), `embed-service`, `form`; or a `service` page on a port of the services range or a service the agent runs, or a `link` page |
| `PageWrite`, `PageImport` | one text file into the draft, or a file or a whole folder from the agent's workspace — a build made with any toolchain, models, data — streamed whatever its size |
| `PageRead`, `PageFiles`, `PageDelete` | the draft's or a version's files |
| `PageCheck` | the page loaded in the agent's browser at 390×844 and 1440×900, in the dark theme and the light scheme: what a vision model saw on each screenshot, console errors, failed requests and missing files, sideways overflow and small tap targets at phone width, whether the bridge shook hands |
| `PagePublish`, `PageRollback`, `PageList` | versions, as above |
| `PageRemove` | a page that was ever published only with `confirm=true`, after the agent asked you |
| `PageShow` | a card with Open in the chat |

`PageCheck` never sees your sign-in. It serves the page from a short-lived address of its own on a
free port of the services range (the only ports the agent's browser may reach), opened by a random
token and closed when the check ends, inside a frame that answers the bridge as the app does. A
page's `/api` requests are answered 401 there, and the result says so. The check is advice: the
agent decides when to publish.

The agent puts a published page on the rail through the `[navigation]` setting (`page:<slug>`).

## Settings

```toml
[pages]
max_page_bytes = 2147483648    # one page's draft, all files together; 0 = no limit
max_files = 0                  # 0 = no limit
keep_versions = 20             # published versions kept for rollback
storage_value_max_bytes = 0    # one stored value; 0 = no limit
```

## Notes

- Pages are served at `/pages/`, outside `/app/`, because the app's service worker answers every
  navigation under `/app/` with the cached app shell.
- Pages are never public. A page opened without a session is told to sign in.
- In Telegram's web client the app itself runs in a third-party frame, where a browser may withhold
  the session cookie from the page's frame; Telegram's desktop and phone apps, the desktop launcher
  and a browser are not affected.
