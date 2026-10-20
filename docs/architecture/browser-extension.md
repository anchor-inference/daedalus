# The browser extension: the operator's own Chrome

The agent normally browses in a Chromium of its own, owned by `browserd` (browser.md). To work in the
operator's own Chrome, where the operator is already signed in, it goes through the Daedalus
extension in `extension/`. The extension relays between Chrome's `chrome.debugger` API and a
`browserd` started with `--env user --backend attach`, which launches no browser and drives the tabs
the extension hands it.

```
                       ┌── this computer: native messaging ── browserd bridge ──┐
Chrome + extension ────┤                                                         ├── browserd serve --env user --backend attach
                       └── a server: wss://<server>/api/browser/relay ──────────┘
```

Chrome only, from version 125: that is where `chrome.debugger` gained `DebuggerSession.sessionId`,
which addresses a flat child session (a frame of another site, a worker) inside a tab. Below it such
frames could not be driven at all, so the manifest's `minimum_chrome_version` is `125`.

## Names that other parts depend on

| What | Value |
|---|---|
| Extension id | `lkhhklicnjlplokpadpccndipomikoaj`, pinned by the public key in the manifest's `key` |
| Native-messaging host | `com.daedalus.browser` |
| Host manifest `allowed_origins` | `chrome-extension://lkhhklicnjlplokpadpccndipomikoaj/` |
| Relay over the network | `wss://<server>/api/browser/relay` |
| Pairing | `POST https://<server>/api/browser/pair` |

The private half of the key is kept by the maintainers, outside the repository. It is needed only to
publish the same id to a store later; an unpacked load needs only the public key already in the
manifest.

## The two transports

**This computer.** The extension calls `chrome.runtime.connectNative("com.daedalus.browser")`. Chrome
starts `browserd bridge` with the extension's origin as its first argument; the bridge checks it
against the pinned id and forwards frames to the `user` daemon. Chrome reports a failed start only as
text, which the extension turns into what the operator should do:

| Chrome's message | Shown as |
|---|---|
| "Specified native messaging host not found." | the connector is not registered: open Daedalus → Settings → Browser |
| "Access to the specified native messaging host is forbidden." | the same: the manifest exists but does not name this extension |
| "Native host has exited." (and the bridge's `bye {reason: "unreachable"}`) | Daedalus is not running on this computer |

**A server.** The operator types the server's address and a one-time code from the app. The
extension posts `{code, profile_id, profile_name, ext_version, chrome_version}` to
`/api/browser/pair` and expects `{token}` back; any 4xx is "the code was not accepted". The token is
kept in `chrome.storage.local` and sent in the first `hello` frame, because the browser's WebSocket
cannot set a header. Only HTTPS is accepted, except for a loopback address. Besides a `bye` frame the
host may close the socket with code `4401` (the token is unknown, expired or revoked: the extension
stops retrying and asks to pair again) or `4409` (another Chrome holds the relay; the close reason is
its name). "Disconnect" forgets the token.

## The relay protocol

The frames, their fields and their directions are fixed by the specification shared with
`browserd/internal/relay`; both sides test against the same golden frames
(`extension/test/frames.json`). What the extension adds to the bare frame list:

- **`bye.reason`** is one of `shutdown`, `replaced`, `busy` (with `name`), `proto` (with the daemon's
  `proto`), `unauthorized`, `unreachable`.
- **`result.error.code`** is one of `refused` (the allowlist or scope said no), `detached` (the tab's
  debugger is not attached, a reattach is pending), `paused` (input into a group the operator
  paused), `cdp` (Chrome refused the command; its text is the message), `chrome` (an extension API
  failed), `bad_args`, `not_found`, `unknown_op`.
- **`tabs {op: "updated"}`** carries either a tab, when its debugger detached (`attached: false`) or
  reattached (`attached: true`), or only a group, when the operator paused it or gave it back
  (`group.paused`).
- **Synthetic detach events.** When a tab's session ends, the extension sends, in the events the
  daemon already understands, a `Target.detachedFromTarget {sessionId}` for each child session and
  then `Inspector.detached {reason}` on the tab's session. Chrome sends neither when the extension
  detaches by itself (`reason: "left"`) nor for the children when the tab's session ends.
- **After `welcome`** the extension replays every tab it still holds as `tabs {op: "joined"}`, so a
  daemon that restarted can take them back or close their group.
- **`tab.create`** always opens `about:blank`, whatever `url` says. The daemon navigates after it has
  attached and set up its navigation guard, so the first page is judged like every other. The result
  is `{tab, session, window, attached}`.
- **`window.ensure {group, title?}`** returns `{window}`; `window.bounds` returns `{window, left, top,
  width, height, state}` and refuses to resize when the agent works in the operator's window.
- **`downloads.search {id}`** answers only downloads that began while the relay was connected, so a
  daemon cannot walk the operator's download history by id.
- **`cookies.names {url}`** answers `{cookies: [{name, domain, path, secure, http_only, same_site,
  session, expires?}]}`, never a value.
- **`Target.setAutoAttach`** is always sent with `flatten: true`: a nested session would never reach
  the extension.

## Scope

A daemon group is a Chrome tab group titled "Daedalus · <title>", with a colour of its own, recorded
in `chrome.storage.session` so that a restarted service worker still knows which tabs are the
agent's. A tab is in scope when the extension holds it as a member, never because of where it sits.

- **The agent's own window.** By default the first tab of a group opens in a new, unfocused window
  of the same profile (the same sign-ins), because a background tab in the operator's window does not
  paint and a screenshot of it waits until it times out. The window's own blank tab is closed when
  the agent's first tab arrives. A popup setting lets the agent work in a tab of the operator's
  current window instead, opened in the background.
- **Granting and revoking.** Dragging a tab into an agent group, or the page's context menu "Let
  Daedalus use this tab", gives it (`joined`); dragging it out takes it back (`left`); closing the
  group, or every tab in it, ends the daemon group (`group_closed`).
- **Pages the agent opened** (`openerTabId` is an agent tab) join as `created`. A browser page (a new
  tab page opened beside the active tab carries an opener too) never joins that way.
- **Focus emulation** is switched on at every attach, so a page in the unfocused window does not see
  itself blurred.
- **Reattach.** A detach that is not the tab closing (the infobar's Cancel, the operator opening
  DevTools on the tab) is followed by reattach attempts after 2, 5, 15, 30 and 60 s; when they run
  out the tab is let go (`left`).

## What the extension refuses by itself

The daemon has its own rules; the extension's allowlist is the second wall, so that a daemon or a
bridge that has been taken over still cannot read the operator's cookies or reach past the agent's
tabs. Allowed: Page, Runtime, DOM, DOMDebugger, DOMSnapshot, Input, Accessibility, Log, Fetch,
Network; Emulation's focus, media and device-metrics methods; Target's `setAutoAttach` and
`detachFromTarget`. Refused always: any method that names a cookie, in any domain;
`Network.clearBrowserCache`; the Storage and Browser domains; every other domain and every other
Target method; and any session that is not `x<tabId>` or `x<tabId>/<child>` of a member tab with a
child session the extension has seen attached.

Beside the debugger, a `declarativeNetRequest` session rule limited to the agent's tab ids blocks
requests to loopback, private, link-local, shared and metadata addresses written as literals or
`localhost`; it holds even while the debugger is detached.

## Telling the operator

- **The badge**: green with the number of agent tabs when connected, grey when idle or connecting,
  red with `!` on an error, blue with `✓` for three seconds when the app ran its own check.
- **The popup**: the transport, the installation's name, the daemon's version, the last heartbeat,
  the agent's tabs and groups, errors in plain words, "Check connection" (`check` → `check_ok` within
  5 s), and per group "Pause", "Give back" and "Stop and close".
- **The border**: a thin frame drawn in a closed shadow root of a `<daedalus-agent-border>` element
  with pointer events off, hidden for the length of a `Page.captureScreenshot`.
- **The operator's own input** in an agent tab (trusted pointer, key and wheel events) is reported as
  `input {tab}`, at most once a second per tab, and not within a second of the daemon dispatching
  input into that tab, since `Input.dispatch*` events reach the page as trusted too.

Every word is in `extension/src/i18n.ts` in Russian and English, chosen by Chrome's interface
language; a test fails on a sentence written anywhere else.
