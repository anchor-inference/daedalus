# The Daedalus browser extension

A Chrome extension (Manifest V3) that lets the agent work in tabs of the operator's own Chrome: tabs
the agent opens in a window of its own, and tabs the operator gives it, each set in a tab group. It
relays between `chrome.debugger` and a `browserd` running `--backend attach`, over native messaging
on this computer or a WebSocket to a server installation. The design, the protocol additions and the
names other parts depend on are in
[`docs/architecture/browser-extension.md`](../docs/architecture/browser-extension.md).

- Extension id: `lkhhklicnjlplokpadpccndipomikoaj` (pinned by `key` in the manifest)
- Native-messaging host: `com.daedalus.browser`
- Chrome 125 or newer

## Build and load

```sh
npm ci
npm run build          # tsc, then the static files: dist/ is the folder Chrome loads
```

Open `chrome://extensions`, switch on Developer mode, choose "Load unpacked" and pick `dist/`. On this
computer the desktop launcher registers the native-messaging host; Chrome reads host registrations
when it starts, so restart Chrome after the first registration.

## Layout

| File | What it is |
|---|---|
| `static/manifest.json` | permissions, the pinned key, `minimum_chrome_version` |
| `src/worker.ts` | the service worker: registers every listener and hands events to the relay |
| `src/relay.ts` | the connection, the DevTools proxy, scope changes, the calls, the badge |
| `src/protocol.ts` | the frames, the host name, session names |
| `src/allowlist.ts` | which DevTools methods may run at all |
| `src/scope.ts` | the agent's groups and tabs, kept in `chrome.storage.session` |
| `src/transport.ts` | native messaging and the server WebSocket, and what their failures mean |
| `src/guard.ts` | the request rule that keeps agent tabs off loopback and private addresses |
| `src/content.ts` | the border on agent tabs and the report of the operator's own input |
| `src/panel.ts`, `static/panel.html` | the side panel: the strip, the framed app at `/app/panel`, the settings view |
| `src/settings.ts` | the settings view: status, the connection check, the group controls, pairing, the name, the window mode |
| `src/bridge.ts` | the side panel's bridge to the framed app: the handshake, the port, the requests it takes |
| `src/capture.ts` | what the side panel attaches from the page: a picture, its text, the selection |
| `src/i18n.ts` | every word, in Russian and English |
| `src/chrome.d.ts` | the part of the extension API used here, typed by hand |

## Tests

```sh
npm run check          # the type check
npm test               # vitest against a fake chrome.* (test/fake-chrome.ts)
```

`test/frames.json` is a byte-for-byte copy of the daemon's golden relay frames
(`browserd/internal/relay/testdata/frames.json`); a test fails when the two differ.

`test/e2e/smoke.py` loads the built extension into a real, unbranded Chromium (branded Chrome ignores
`--load-extension` since 137) with a stub native host, and checks hello and welcome, ping and pong,
the app's check flashing, the side panel's check confirmed, a tab created and driven, and a cookie
read refused by the extension itself. The side panel against the real host, the real daemon and a
scripted model is in `tests/integration/test_user_chrome_real.py`. It needs Playwright:

```sh
npm run build && python test/e2e/smoke.py
```
