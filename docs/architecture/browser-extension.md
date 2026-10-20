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
| "Specified native messaging host not found." | the connector is not registered: open Daedalus → Settings → Environments |
| "Access to the specified native messaging host is forbidden." | the same: the manifest exists but does not name this extension |
| "Native host has exited." (and the bridge's `bye {reason: "not_running"}`) | Daedalus is not running on this computer |

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
`browserd/internal/relay`, whose `testdata/frames.json` holds one golden frame of every kind. The
extension keeps a byte-for-byte copy in `extension/test/frames.json`, and a test fails when the two
differ. What the extension does with them, beyond the bare list:

- **`bye`** ends the relay and the side panel explains its `reason` in its own words: `shutdown`,
  `replaced`, `another_relay` (the other Chrome's name is the end of `message`), `proto` (with the
  daemon's `proto`: lower than the extension's means "update Daedalus", higher "update the
  extension"), `lost`, `bad_frame`; from the bridge `not_running` and `unavailable` (a daemon that
  takes no relay); from the host `unauthorized` (the pairing expired or was revoked). Each but the
  last is followed by a reconnect, slower for a busy relay or a version mismatch.
- **`result.error.code`** is one of `refused` (the allowlist, a tab outside the agent's groups, or
  input into a group the operator paused), `no_tab` (a call named a tab that is gone or not the
  agent's), `cdp` (Chrome refused the command; its text is the message), `detached` (the tab's
  debugger is not attached, a reattach is pending), `failed` (anything else).
- **Tabs in frames** are `{id, group, opener?, url?, title?}` with empty fields left out; `removed`
  and `left` carry the id alone, `updated` the new address and title, and `group_closed` the group's
  id as a string.
- **Synthetic detach events.** When a tab's session ends, the extension sends, in the events the
  daemon already understands, a `Target.detachedFromTarget {sessionId}` for each child session and
  then `Inspector.detached {reason}` on the tab's session. Chrome sends neither when the extension
  detaches by itself (`reason: "left"`) nor for the children when the tab's session ends. When a
  reattach succeeds the tab is reported `joined` again, so the daemon adopts the new session as it
  would a tab the operator gave.
- **After `welcome`** the extension replays every tab it still holds as `joined`, so a daemon that
  restarted can take them back or close their group.
- **Pause.** The protocol has no pause of its own. While the operator holds a group paused, the
  extension reports `input` for each of its tabs at once and every 2 s, which keeps the daemon's
  human-control lease alive, and refuses the daemon's `Input.*` commands on those tabs itself.
- **`tab.create`** always opens `about:blank`, whatever `url` says. The daemon navigates after it has
  attached and set up its navigation guard, so the first page is judged like every other. The value
  is `{tab: {id, group, url}}`.
- **`window.ensure {group}`** answers `{window, tabs}`, the tabs already in the group, so a daemon
  that lost track of them starts from what is there. `window.bounds` answers `{bounds: {left, top,
  width, height}}` and refuses to resize when the agent works in the operator's window.
- **`downloads.search {id}`** takes the DevTools guid of `Page.downloadWillBegin`. The extension
  remembers the address each such event of an agent tab named and finds the newest
  `chrome.downloads` item of that address begun no earlier than shortly before it; the answer is
  `{path, state, bytes}`. A guid no agent tab announced finds nothing, so the operator's download
  history stays out of reach.
- **`cookies.names {url}`** answers `{cookies: [{name, domain, path, expires, secure, http_only,
  same_site, session}]}`, never a value.
- **`notify {text}`** answers `{}`.
- **`Target.setAutoAttach`** is always sent with `flatten: true`: a nested session would never reach
  the extension.

## Scope

A daemon group is a Chrome tab group titled "Daedalus · <title>", with a colour of its own, recorded
in `chrome.storage.session` so that a restarted service worker still knows which tabs are the
agent's. A tab is in scope when the extension holds it as a member, never because of where it sits.

- **The agent's own window.** By default the first tab of a group opens in a new, unfocused window
  of the same profile (the same sign-ins), because a background tab in the operator's window does not
  paint and a screenshot of it waits until it times out. The window's own blank tab is closed when
  the agent's first tab arrives. A setting of the side panel lets the agent work in a tab of the operator's
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

- **The badge**: amber with the number of conversations waiting for the operator, else green with
  the number of agent tabs when connected, grey when idle or connecting, red with `!` on an error,
  blue with `✓` for three seconds when the app ran its own check.
- **The side panel's settings** (the toolbar button opened a popup with these before the panel
  existed): the transport, the installation's name, the daemon's version, the last heartbeat, the
  agent's tabs and groups, errors in plain words, "Check connection" (`check` → `check_ok` within
  5 s), per group "Pause", "Give back" and "Stop and close", pairing, the browser's name and the
  window mode.
- **Notifications**: a conversation in this Chrome that waits for the operator or finished, unless
  the operator is looking at the panel of the window they are in; a click opens the panel on it and
  shows the agent's tab.
- **The border and the agent's cursor**: a thin pulsing frame and a cursor labelled "Daedalus", drawn
  in a closed shadow root of a `<daedalus-agent-border>` element with pointer events off, so the page's
  scripts cannot read them and hit testing passes through. The cursor glides to the point of each
  `Input.dispatchMouseEvent`, drag or touch the daemon sends into the tab (viewport CSS pixels, the
  coordinates a fixed box takes), rings on a press and shows typing for key input; a frame of another
  site is left out. Both are hidden for the length of a `Page.captureScreenshot`: the relay waits for
  the page to say it painted (at most 400 ms) before the screenshot, so the agent never sees its marks.
- **The microphone**: a side panel cannot show Chrome's permission question, so when dictation in the
  panel is refused the frame asks for `mic.grant` and the extension opens its own `mic.html` in a tab,
  where Chrome asks once; the frame then has the permission through `allow="microphone"`.
- **The operator's own input** in an agent tab (trusted pointer, key and wheel events) is reported as
  `input {tab}`, at most once a second per tab, and not within a second of the daemon dispatching
  input into that tab, since `Input.dispatch*` events reach the page as trusted too.

Every word is in `extension/src/i18n.ts` in Russian and English, chosen by Chrome's interface
language; a test fails on a sentence written anywhere else.

## The side panel

The toolbar button and its shortcut (Alt+Shift+D, changeable at `chrome://extensions/shortcuts`)
open the side panel; Chrome opens a panel only on the operator's gesture, so it never opens by itself.
There is one panel per window, and it follows that window's active tab. The page's context menu has
"Ask Daedalus about this page", which opens the panel on a new conversation for that page.

The panel page (`panel.html`, an extension page, so privileged) draws a 32 px strip with the
connection's state and the settings above, and frames the host's own app at `/app/panel`. The app is
framed rather than bundled so that it always speaks the API of the server it talks to: the extension
on a laptop is updated on its own schedule. It shows the conversations that use this Chrome and
nothing else, starts new ones (born in this Chrome, the consent summary shown once, the tab in front
of the operator given to the new conversation's group before its first message), and answers the
agent's pending actions inline (Enter allows once, Ctrl+Enter allows the like for the conversation,
Esc refuses).

- **The credential**: a panel token the host mints for the connected Chrome and hands over the relay
  (`panel.grant`), kept in `chrome.storage.session`, passed to the frame in the handshake only, never
  in an address. The host keeps its SHA-256, lets it lapse after twelve hours unused, and revokes it
  when the device is unpaired or every browser session is ended. It opens `/api/panel/*` alone: the
  conversations switched to the operator's Chrome, and nothing else of the installation (a
  conversation outside them answers 404, every other route 401).
- **Framing**: `/app/panel` answers `frame-ancestors chrome-extension://lkhhklicnjlplokpadpccndipomikoaj`;
  the rest of the app is framed only by itself and Telegram's web client.
- **The bridge** (`src/bridge.ts`, and `miniapp/src/sidepanel/bridge.ts` on the app's side): the frame
  posts `DAEDALUS_PANEL_READY {proto, nonce}` to the extension's origin; the panel accepts it only from
  the frame's own window and the host's origin and answers `DAEDALUS_PANEL_HELLO` with the nonce, the
  credential and a `MessageChannel` port, addressed to the host's origin. Each READY starts a new
  generation and closes the old port, so a superseded load's late request is dropped. On the port the
  frame may ask for the current tab, a picture of the visible tab, the page's text, the selection,
  giving the active tab to a conversation, showing a conversation's tab, pausing its group, opening
  the app, and a newer credential. The reads of the page and giving a tab need the operator's click
  in the panel a moment before (user activation spreads from the frame to the panel page), so a frame
  that was taken over cannot photograph or read a page silently.
