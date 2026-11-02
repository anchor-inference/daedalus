# The desktop: the daemon and its protocol

Computer use lets the agent see and drive the operator's own desktop in the native application. The
desktop is owned by `deskd`, a small daemon written in Go (`deskd/`), a child of the desktop launcher
like `ptyd` and `browserd`. It captures screens, sends synthetic mouse and keyboard input and lists
the windows and the apps behind them, and it enforces the rules that must hold even when the host
hangs: no input outside a lease the operator granted, nothing to Daedalus's own windows, nothing on
a locked screen, a pause the moment the operator touches the machine, and a kill switch.

`deskd` knows displays, windows, apps, pixels and input events; it knows nothing about models,
sessions' contents or policies. The host decides what the agent may ask for and asks; the operator,
through the launcher, decides whether the agent may act at all.

This document is the contract the daemon, the host and the launcher are tested against. A section
marked *not yet* describes a part whose names are fixed but which the daemon does not serve yet.

## Running it

```
deskd serve --run-dir <dir> --state-dir <dir> [flags]
deskd selftest [--verbose]
deskd version
```

| Flag | Default and meaning |
|---|---|
| `--run-dir` | the run directory: endpoint, host token, socket (required) |
| `--state-dir` | the operator's token and the audit (required) |
| `--listen` | `unix` (a socket in the run directory), or `tcp:127.0.0.1:<port>`; on Windows `tcp:127.0.0.1:0`. Nothing else: the desktop's socket never leaves the machine |
| `--backend` | `native` (the system's desktop) or `fake` (one in memory; see The fake desktop) |
| `--scene` | the fake desktop's displays and windows, a JSON file (`--backend fake` only) |
| `--config` | a JSON file with the limits and the protected apps (Configuration); unknown keys are refused |
| `--operator-token-file` | where the operator's token is written; `<state-dir>/operator.token` by default. It may not be in the run directory |
| `--audit-file` | the audit, JSON lines; `<state-dir>/audit.jsonl` by default, `off` for none |
| `--log-file`, `--log-level` | the daemon's own JSON-lines log (stderr, `info`) |
| `--ax-bin` | the accessibility tool, agent-desktop (see The accessibility tree): its path; `agent-desktop` (`agent-desktop.exe`) beside the daemon's own executable when not given and one is there; `off` for none; `fake` for the built-in fake (`--backend fake` only). No value stops the daemon from starting: one it cannot use leaves the tree unavailable |

`SIGTERM` or `SIGINT` stops the daemon: whatever the agent holds down is released (as by `stop`),
then the endpoint, the token, the operator's token and the socket are removed. On macOS the daemon
must stay a direct child of the launcher, itself a child of the application: the system attributes
its privacy grants to the responsible process, and a daemon started through launchd would have none.

### Configuration

```json
{
  "actions_per_second": 4, "action_burst": 4, "lease_actions": 200,
  "default_lease_ms": 1800000, "max_lease_ms": 14400000,
  "human_quiet_ms": 3000, "max_type_chars": 2000, "max_wait_ms": 10000,
  "screenshot_ttl_s": 600, "max_edge": 1568,
  "protected": [{"bundle_id": "…"}, {"exe_name": "Daedalus"}, {"wm_class": "daedalus"}]
}
```

Every key is optional; the values above are the defaults. `human_quiet_ms: 0` leaves the agent
paused after the operator's input until the operator resumes it. A `protected` entry names an app by
any of `bundle_id`, `exe_path`, `exe_name` (the executable's base name, compared without case and
without `.exe`), `wm_class` (either half of X11's `WM_CLASS`, without case) and `pid`; every field an
entry sets must match, and an empty entry matches nothing. **The launcher must list Daedalus itself**
(the shell's bundle id on macOS, its executable elsewhere): that entry is what keeps the agent from
pressing "allow" on its own approval card.

## Run directory, tokens and roles

```
<run>/endpoint       "unix:deskd.sock" or "tcp:127.0.0.1:<port>"
<run>/token          the host's token: 64 hex characters, mode 0600, new at every start
<run>/deskd.sock     mode 0600
<run>/deskd.lock     held for the daemon's lifetime
<state>/operator.token  the operator's token, mode 0600 (on Windows an owner-only access list), new at every start
```

The files, the handshake and the framing are `ptyd`'s (terminals.md, Run directory and handshake).
The first frame on channel 0 carries a token. The daemon accepts two, and **the token decides the
role**; no request can claim one:

- the **host** token (`<run>/token`): the Daedalus host, for the agent's actions within a lease;
- the **operator** token (`operator.token`): the launcher, speaking for the shell's consent dialog,
  Stop button, tray item and permissions page. Only it can grant a lease or lift a pause.

The host is told the run directory only (`COMPUTER_HOST_DIR`); the operator's token is in the state
directory, which the launcher keeps from the agent as it keeps its other local state.

A good token is answered with `hello {version, protocol, instance, role: "host" | "operator",
platform{os, session_type, units}}`. `protocol` is 1. The host refuses a protocol it does not know.

| Method | host | operator |
|---|---|---|
| `info`, `lease.status`, `apps.list`, `events.subscribe`, `events.unsubscribe` | yes | yes |
| `screenshot`, `act`, `cursor`, `apps.focus` | yes, within its lease | no |
| `ax.snapshot`, `ax.find`, `ax.get`, `ax.act` | yes, within its lease | no |
| `lease.grant`, `resume`, `permissions.request`, `preview` | no | yes |
| `lease.revoke` | its own session's lease | any |
| `stop` | yes | yes |
| `fake.simulate` (the fake desktop only) | no | yes |

`stop` and `lease.revoke` are open to the host because they only take power away: the app's Stop
button and the agent's own hand-back reach the daemon through the host. A method the role may not
call is refused with `1208`.

## Framing and errors

```
frame   := u32be length | u32be channel | payload     (length = 4 + len(payload), at most 1 MiB + 4)
channel 0 : one UTF-8 JSON-RPC 2.0 message per frame
```

No other channel is used yet; a live view of the desktop will use browserd's view frames (*not
yet*). Requests run concurrently, at most 256 in flight per connection; input is serialised inside
the daemon, so two actions never interleave their events. Parameters are decoded strictly: an
unknown field is `-32602`. Errors use the JSON-RPC codes, `ptyd`'s `1001 not_found`, `1003 limit`,
`1007 unsupported`, plus:

| Code | Name | Meaning; `error.data` |
|---|---|---|
| 1101 | `human_driving` | the operator touched the mouse or keyboard during the lease; `{paused, since, last_input, resumes_at?}`. Actions are refused until the quiet period passes with no input (`resumes_at`) or the operator resumes |
| 1200 | `no_lease` | `{reason: "none" \| "expired" \| "stopped" \| "revoked" \| "superseded" \| "locked" \| "other_session"}` |
| 1201 | `stale_screenshot` | the screenshot id cannot be used; `{screenshot_id, reason: "unknown" \| "other_lease" \| "old" \| "displays_changed" \| "window_moved" \| "window_gone"}`. Take a new screenshot |
| 1202 | `out_of_scope` | the point, the window or the display is outside the lease's scope, or the point is off the screenshot; `{scope, point?, window?}` |
| 1203 | `protected_target` | an app on the protected list (Daedalus itself); `{app, point?}` |
| 1204 | `secure_desktop` | a locked screen, a secure desktop or a running screen saver; `{reason: "lock_screen" \| "secure_desktop" \| "screen_saver", lease_ended}`. Finding one ends the lease (`locked`, see A locked screen); `lease_ended` says whether this call ended one |
| 1205 | `no_permission` | the system has not granted what the call needs; `{which: "screen" \| "accessibility" \| "input_monitoring", hint}` |
| 1206 | `rate` | `{limit: "per_second", retry_after_ms}` or `{limit: "lease_actions", max}` |
| 1207 | `unreachable` | the system silently drops input to that window (Windows' UIPI in front of a program run as administrator); `{reason: "elevated", app?}` |
| 1208 | `operator_only` | the method needs the operator's token; `{role}` |
| 1209 | `ax_unavailable` | no accessibility tree for this call; `{reason: "off" \| "missing" \| "version" \| "failed" \| "crashed" \| "unsupported" \| "no_window"}` (see The accessibility tree). Use screenshots and points instead |
| 1210 | `stale_ref` | the element reference cannot be used; `{ref, reason: "unknown" \| "old" \| "window_gone" \| "changed" \| "expired" \| "not_found"}`. Take a new `ax.snapshot` |
| 1211 | `ambiguous_target` | the reference, or the window, matches more than one live element or accessible window; `{ref?, window?, candidates?}`. Narrow it with `ax.find` or `root` |
| 1212 | `blocked_key` | a chord that closes, locks or leaves the session, refused whatever the host asks; `{keys, reason: "ends" \| "leaves", why}` (see Blocked keys) |
| 1213 | `ax_failed` | the accessibility tool ran the call and it failed: `{reason: "rejected" \| "contradicted" \| "timeout" \| "unresponsive" \| "internal", delivery, post_state?}`. `contradicted` is an action the element took whose result reads back otherwise |

Every refusal happens before any input is sent, except the ones an action meets half way (a stop,
the lease's end, a locked screen, the operator's hand): those cut the action short at its next event
and release whatever it held down. Every refusal of `act` and `ax.act` says which of the two it was
in `data.delivery` (see Delivery).

## Coordinates

Three spaces meet, and the daemon converts between them in one place (`deskd/internal/geom`):

- **Desktop units** are what the system's input takes: X11 root pixels; Windows physical pixels (the
  daemon is per-monitor DPI aware, so the virtual screen is one physical space); macOS global points.
  `info.platform.units` says `pixels` or `points`. Displays may start at negative coordinates.
- **Captured pixels**: a display's `pixels_per_unit` to a unit (2 on a Retina display, 1 elsewhere).
- **Screenshot pixels**: the picture the model sees, captured pixels scaled down (never up) so the
  long edge is at most `max_edge`. The daemon scales, not the model's provider: a provider that
  resizes on its side shifts every coordinate read off the picture.

A screenshot's answer carries its `region` (desktop units), `width` and `height`. A point `(x, y)` of
the picture is the centre of the area its pixel covers:

```
desktop_x = region.x + (x + 0.5) * region.w / width
desktop_y = region.y + (y + 0.5) * region.h / height
```

`(0, 0)` is the top-left pixel, a coordinate like any other. A point off the picture (`x < 0`,
`x >= width`, …) is refused with `1202`. Taking the centre leaves no rounding to disagree about.
Backends that take whole numbers use the floor of the desktop point; macOS takes the fraction.

**Every action that names a point names the screenshot it was read from** (`screenshot_id`), and
the daemon converts with that screenshot's own geometry. An id is good while all of these hold:

- it is one of the last 16 the daemon handed out, under the current lease;
- it is younger than `screenshot_ttl_s`;
- the displays have not changed since (any change of a display's bounds, scale or count invalidates
  every id);
- for a screenshot of a window, the window still exists with the same bounds.

Otherwise the action is refused with `1201` and the reason.

## Objects

- **Display** `{id, name, bounds{x, y, w, h}, primary, pixels_per_unit, scale}`. `name` is for people:
  the monitor's own name on Windows (from its EDID, "DELL U2720Q", with the display's number added when
  two match) or "Display N" when Windows has none; macOS's screen name ("Built-in Retina Display");
  the XRandR output on X11 ("HDMI-1"). `scale` is the
  factor the system shows (Windows' 125 %, macOS's backing scale, 1 on X11), for people; the
  arithmetic uses `pixels_per_unit`. The primary display is first.
- **App** `{id, name, pid?, bundle_id?, exe_path?, wm_class?, elevated?}`. `id` is stable for the
  daemon's life: the bundle id on macOS, otherwise `pid:<n>` (or `class:<wm_class>` on X11 when the
  window names no process).
- **Window** `{id, title, bounds, app: App, focused, minimized?}`. `id` is the system's: X11 and
  Windows `0x<hex>`, macOS the decimal window number. `bounds` include the frame the operator sees.
- **Lease** `{id, session, granted_at, expires_at, scope, max_actions, actions}`. One at a time: a
  grant ends the previous lease (`superseded`). The host cannot create, extend or widen one.
- **Scope** `{kind: "window" | "app" | "display" | "desktop", window?, app?, display?}`:

  | Kind | Screenshots | Points | Keys |
  |---|---|---|---|
  | `window` | that window (the default target) | inside that window, or a window of the same process (its dialogs) | when that window, or one of its process, has the keyboard |
  | `app` | any display, any window of the app | on a window of the app | when a window of the app has the keyboard |
  | `display` | that display, or a window mostly on it | on that display | when the window with the keyboard is mostly on that display |
  | `desktop` | anything | on any display | anywhere |

  A window scope is the safest and the one to start with. A protected app is outside every scope.
- **Permissions** `{screen, accessibility, input_monitoring}`, each `granted | denied | unknown |
  not_applicable`. Only macOS has any; elsewhere all three are `not_applicable`.
- **Secure** `{locked, reason?}`.
- **Human** `{paused, since?, last_input?, resumes_at?}`.

## Methods

| Method | Params → result |
|---|---|
| `info` | → `{version, protocol, instance, role, os, arch, pid, started_at, uptime_s, platform{os, session_type: "x11" \| "win32" \| "quartz" \| "fake", units}, backend, displays: [Display], permissions, secure, lease: LeaseStatus, human: Human, limits{…}, ax: {available, reason?, version?}}`. It never raises a system prompt. `ax` says whether the accessibility tree is served; `reason` is the 1209 reason when it is not |
| `screenshot` | `{session, display? \| window?, max_edge? (64–8192, default 1568), format?: "jpeg" \| "png" (jpeg), quality? (1–100, 80)}` → `{screenshot_id, format, data (base64), width, height, display, window?, region, units_per_pixel[2], pixels_per_unit, scale, units, cursor?[2]}`. With neither `display` nor `window` the target is the lease's window or display, else the primary display. `cursor` is the pointer in picture pixels when it is on the picture. A JPEG too large for a frame steps its quality down; a PNG too large is `1003` (ask for JPEG or a smaller `max_edge`) |
| `act` | see Actions |
| `preview` | `{display? \| window?, max_edge? (64 up; default 480, larger asks get 960)}` → `{image (base64 JPEG), width, height, display, window?}`: a thumbnail for the operator choosing what to grant. No lease is needed and no screenshot id is handed out, so nothing can be aimed with it; it is not audited. With neither, the primary display. A protected window is `1203`, a locked screen `1204`. On Windows a window is drawn by itself (`PrintWindow` with full content), so a half-covered window shows whole; elsewhere, or when that draws nothing, it is the screen region the window covers |
| `cursor` | `{session, screenshot_id?}` → `{x, y, in_screenshot?[2]}` |
| `apps.list` | `{session?}` → `{apps: [App + {protected?, reachable, in_scope, windows: [{id, title, bounds, display, focused, minimized?, in_scope, order}]}], foreground?}`, front to back. The host must hold the lease and never sees protected apps; the operator sees everything, to choose a scope from |
| `apps.focus` | `{session, app? \| window?}` → `{window}`: raises the window (an app's frontmost) and gives it the keyboard. Judged like an action (scope, protected, elevated) and counted as one |
| `lease.grant` | `{session (1–128 chars), scope, ttl_ms? (1000–max_lease_ms, default default_lease_ms), max_actions? (lowers lease_actions)}` → `{lease}`. A window or app scope must name one on screen; a protected one is `1203`. It also lifts a pause, since a grant is the operator's consent now |
| `lease.revoke` | `{session?}` → `{revoked}` |
| `lease.status` | → `{lease: {active, lease?, remaining_ms?, last_ended?}, human}` |
| `stop` | → `{stopped: true}`: cancels the action in flight at its next event, releases every button and key the daemon holds, ends the lease (`stopped`). Nothing more happens until a new grant |
| `resume` | → `{resumed}`: ends a pause for the operator's input |
| `permissions.request` | `{which}` → `{which, state, permissions, hint}`: asks the system, which may show its prompt (macOS) |
| `events.subscribe` | `{after_seq?}` → `{instance, from_seq, resync}`, as `ptyd`'s |
| `events.unsubscribe` | → `{}` |
| `ax.snapshot`, `ax.find`, `ax.get`, `ax.act` | see The accessibility tree |
| `fake.simulate` | see The fake desktop |

## Actions

`act {session, action, …}` → `{action, delivery: "delivered", points?: [[x, y]] (desktop units),
target?: {window, app}, cursor[2], duration_ms, actions_left}`.

| Action | Params | What happens |
|---|---|---|
| `move` | `screenshot_id, x, y` | the pointer moves there |
| `click` | `screenshot_id?, x?, y?, button?: "left" \| "right" \| "middle", count? (1–3), mods?: ["ctrl" \| "alt" \| "shift" \| "super"]` | moves there (or clicks where the pointer is, still judged where it lands), holds the modifiers, presses and releases `count` times 40 ms apart |
| `drag` | `screenshot_id, path: [{x, y}] (2–32 points)` | the left button goes down at the first point and up at the last, the pointer stopping every 12 units along the way (at most 60 stops a segment) |
| `scroll` | `screenshot_id?, x?, y?, dx?, dy?` (notches, at most 50; `dy > 0` down, `dx > 0` right) | moves there if given, then one notch at a time |
| `type` | `text` (1–`max_type_chars` characters) | typed whatever the keyboard layout: line breaks press Enter, tabs press Tab. Never through the clipboard |
| `key` | `keys` (chords separated by spaces), `repeat?` (1–50) | each chord: modifiers down, key down and up, modifiers up |
| `wait` | `ms` (1–`max_wait_ms`) | nothing; cut short by a stop |

Key names: modifiers `ctrl` (`control`), `alt` (`option`), `shift`, `super` (`cmd`, `meta`, `win`);
named keys `enter` (`return`), `tab`, `escape`, `backspace`, `delete`, `insert`, `home`, `end`,
`pageup`, `pagedown`, `up`, `down`, `left`, `right`, `space`, `capslock`, `printscreen`, `menu`,
`f1`–`f24`; any single character. Case, `_` and `-` are ignored, so xdotool's `Page_Down`, OpenAI's
`ARROWLEFT` and Playwright's `Meta` all parse. A capital letter alone means Shift and the letter.

An action is judged in this order, and nothing reaches the desktop until every step passes:

1. the parameters;
2. the lease: active, not expired, held by this `session` (`1200`);
3. for `key`, every chord against the blocked keys (`1212`);
4. the pause for the operator's hand (`1101`);
5. the locked screen or secure desktop (`1204`), which also ends the lease;
6. the screenshot id and the conversion of every point (`1201`, `1202`);
7. where each point lands: the window under it, protected (`1203`), elevated (`1207`), outside the
   scope (`1202`);
8. for `type`, `key` and a click with modifiers, the window with the keyboard, judged the same way;
9. the rate (`actions_per_second`, `action_burst`) and the lease's `max_actions` (`1206`). `wait`
   is not counted.

### Blocked keys

Some chords are refused whatever the host asks, because after them no screenshot can show the
operator what was lost: the session is locked, logged out or switched away, the machine shuts down,
or the app in front is gone. Others only take the keyboard away from the window in front; they are
refused unless the lease covers the whole desktop, where a click could leave the window as well. A
rule matches a chord with its key and at least its modifiers, in any order, so `shift+alt+f4` is
still `alt+f4`; a sequence with one refused chord sends none of it. On macOS `super` is Command and
`alt` is Option.

| System | Refused under any lease (`reason: "ends"`) | Refused unless the lease is the desktop (`reason: "leaves"`) |
|---|---|---|
| Linux | `alt+f4`, `ctrl+alt+backspace`, `ctrl+alt+delete`, `ctrl+alt+f1`–`f12` (another virtual terminal), `super+l`, `ctrl+alt+l`, `ctrl+alt+shift+pageup` / `pagedown` (KDE's restart and shut down without asking) | `alt+tab`, `super+tab`, `super` alone, `super+d`, `alt+f1`, `alt+f2`, `ctrl+alt+t` |
| Windows | `alt+f4`, `ctrl+alt+delete`, `ctrl+alt+end` (the same in a remote session), `super+l`, `ctrl+shift+escape`, `super+x` | `alt+tab`, `super+tab`, `alt+escape`, `super` alone, `super+d`, `super+m`, `super+r`, `super+e` |
| macOS | `super+q`, `ctrl+super+q`, `super+shift+q`, `super+alt+escape`, `super+shift+backspace` | `super+tab`, `super+space`, `super+h`, `super+m`, `ctrl+up`, `ctrl+left`, `ctrl+right` |

The fake desktop refuses every system's chords. The refusal's `why` says what the chord does, and the
host passes it to the model with the advice to take another way. `type` cannot send a chord: its line
breaks and tabs are plain Enter and Tab.

### Delivery

Every answer of `act` says whether its input may have reached the desktop, so the host knows when
sending the action again is safe:

| `delivery` | Meaning |
|---|---|
| `delivered` | the result: every event of the action was taken by the system. Physical input is never read back, so this says nothing of what the program under it did; the screenshot is the judge |
| `not_delivered` | in a refusal's `data`: no event was sent, because the action was refused before its first or stopped before it (a `wait` cut short sent nothing) |
| `unknown` | in a refusal's `data`: the action failed or was cut short after its first event, so part of it may have landed (half a typed text, a button that went down and was released by the stop) |

The host sends an action again by itself only on `not_delivered`, and only for the per-second rate
(`1206 {limit: "per_second"}`), after the `retry_after_ms` the daemon names, up to two times and
never for a wait longer than 2 s. Everything else goes to the model; on `unknown` it is told that
part of the action may have happened and to look before repeating it. A daemon that does not send
`delivery` counts as `unknown`.

## The accessibility tree

Besides pixels and input events, the daemon reads the tree the system's accessibility layer keeps of
a window (macOS's AX, Windows' UI Automation, Linux's AT-SPI) and acts on its elements by reference:
a button is pressed through its own press action, a text field takes its value through its text
interface. Nothing moves the pointer, takes the keyboard or brings the window forward, so an element
of a granted window is reachable while the window is covered or in the background (the operator's
decision; the border still shows what is granted). It is far more accurate than a point read off a
picture, and its answers are text, not images. Apps that expose a poor tree (games, canvases, remote
desktops, some Qt and Java programs) still need screenshots and points; the two work side by side
under the same lease.

The tree comes from **agent-desktop** (vercel-labs, Apache-2.0), a third-party command-line tool the
desktop application ships pinned beside the daemon. The daemon runs it as a child, one call at a
time, reads its JSON answer and passes on only what this section names. It is never the authority:
every rule below is the daemon's and is checked before the tool runs.

### Availability

At start the daemon finds the binary (`--ax-bin`) and runs `agent-desktop version`. It runs only the
release it was checked against, **1.0.2**, whose JSON envelope is 2.5: the tool changes its payloads
between minor versions, so a newer one waits until the daemon has been checked against it, and the
envelope must still be 2.x at 2.5 or later. The binary's bytes are not hashed by the daemon: the
release's checksums and the operator's signature cover them before install, macOS signs the binary
after the launcher is built, and a hash kept beside the binary stops no one who can replace both.
`info.ax` says the outcome. Without a usable binary every
`ax.*` call is refused with `1209` and its reason, and everything else works as before:

| `reason` | Meaning |
|---|---|
| `off` | `--ax-bin off`, or no binary beside the daemon and none named |
| `missing` | the named binary is not there or is not a file |
| `version` | `version` answered with another version than 1.0.2, or an envelope older than 2.5 |
| `failed` | `version` did not answer with JSON at all: the binary does not run here (for example a glibc older than 2.35 on Linux) |
| `crashed` | this call's child exited without an answer; the next call tries again |
| `unsupported` | the desktop is one the tree is not served on (the fake desktop without `--ax-bin fake`, or `--ax-bin fake` on a real one) |
| `no_window` | this window exposes no accessibility tree, or none the daemon can match to it (see Windows) |

A missing permission is `1205 {which: "accessibility"}`, as for input. On macOS the tool, a child of
the daemon, is covered by Daedalus's own Accessibility grant.

### Methods

Only the host calls them, and only while its session holds the lease. `window` is the daemon's window
id (Objects), never the tool's. `timeout_ms` is the tool's own deadline (500–30000; 3000 for a
snapshot, 5000 otherwise); the child is killed 5 s after it.

| Method | Params → result |
|---|---|
| `ax.snapshot` | `{session, window? \| root?, surface?: "window" \| "menu" \| "sheet" \| "popover" \| "alert", depth?: "full" \| "skeleton", interactive? (true), bounds? (false), timeout_ms?}` → `{snapshot_id, window, complete, ref_count, tree: Node}`. With neither `window` nor `root`, the lease's window (a window lease) or `-32602` (any other). `root` is a reference from an earlier snapshot: its subtree is read again, inside that snapshot, and replaces the references the last read of that root gave (`window` and `surface` may not be given with it). `interactive` keeps only elements one can act on and drops empty wrappers; `skeleton` stops at depth 3 and gives `children_count` where it cut. `complete: false` is a tree the deadline cut short; nodes whose children were cut carry `truncated` |
| `ax.find` | `{session, window? \| root?, role?, name?, value?, text?, exact? (false), limit? (1–200, 50), timeout_ms?}`, at least one of `role`, `name`, `value`, `text` → `{snapshot_id, window, matches: [Match], roles_present?}`. `name` and `text` match loosely unless `exact`; `text` also matches an element whose subtree holds the text. A match without `ref` is text one can read but not act on. `roles_present` lists the roles in the searched tree when a `role` matched nothing |
| `ax.get` | `{session, ref, property: "text" \| "value" \| "title" \| "bounds" \| "role" \| "states" \| "visible" \| "enabled" \| "checked" \| "focused" \| "expanded" \| "selected"}` → `{ref, property, value, live?, applicable?, secure?}`. The first six read the element (live where the system allows; `live` says so for `bounds`), the rest are booleans (`applicable: false`: the system could not tell, which is not `false`). A secure field's `text` and `value` are `null` with `secure: true` |
| `ax.act` | `{session, ref, action, text?, direction?, amount?, timeout_ms?}` → `{action, ref, window, delivery, verified, post_state?, surfaces?, actions_left, duration_ms}` (see below) |

**Node** `{ref?, role, name?, value?, description?, states?, actions?, secure?, bounds?,
children_count?, truncated?, children?}`. Only elements one can act on have a `ref`. `states` are the
tool's words (`focused`, `enabled`, `checked`, `expanded`, `selected`, `secure`, …); `actions` what
the element offers. `bounds` `{x, y, w, h}` only when asked for, in desktop units as the system's
accessibility layer reports them, for orientation only: a point for `act` still comes from a
screenshot. **Match** `{ref?, role, name?, value?, states?, secure?, bounds?}`.

**A secure field** (a password box: the state `secure`, or a role naming a password) never shows its
value: the tool withholds it, and the daemon removes it again from trees, matches, `get` and
`post_state`, and marks the element `secure: true`, so the host can treat any action on it as
sensitive.

**References** are `@<snapshot_id>:e<N>`, handed back exactly as given. The daemon keeps, per lease,
every snapshot it took: which window it came from and every reference it handed out, with the
element's role and whether it is secure. A reference is refused with `1210` when:

- the daemon never handed it out under this lease (`unknown`): a reference from an earlier lease, from
  another program's run of the tool, or made up;
- its snapshot is older than `screenshot_ttl_s` (`old`), or not among the last 32 of the lease;
- its window is gone (`window_gone`);
- the tool can no longer find it in the live tree (`changed`), forgot the snapshot (`expired`) or
  finds no element at all (`not_found`).

Every new lease starts with none, and when a lease ends the tool's own state for it is deleted.

### Element actions

| `action` | Params | What happens |
|---|---|---|
| `click` | — | the element's own press or activate action |
| `set_value` | `text` | the value written through the element's value interface: any script, whatever the keyboard layout. It may skip some of the program's callbacks; `type` does not |
| `clear` | — | the element's value emptied |
| `type` | `text` | the text inserted at the element's caret through its text interface, never as keystrokes. Windows has no such path: refused with `1007 {reason: "needs_pointer"}`, use `set_value` |
| `select` | `text` (the option as shown) | an option of a list, combo box or menu button chosen |
| `toggle`, `check`, `uncheck` | — | a check box or switch changed; `check` and `uncheck` do nothing to one already so |
| `expand`, `collapse` | — | a tree item, disclosure or accordion opened or closed |
| `scroll` | `direction?: "up" \| "down" \| "left" \| "right"` (down), `amount?` (1–50, 3) | a scroll area moved through its scroll interface. Linux has no such path (`1007 needs_pointer`): scroll with `act` |
| `scroll_to` | — | the element brought into view inside its scroll area |
| `focus` | — | the element given the keyboard focus inside its window, where the system allows it without input (macOS); elsewhere `1007 needs_pointer` |

`text` is 1–`max_type_chars` characters. The result:

- `delivery` is `delivered`, or `not_delivered` when the element was already as asked and nothing
  was sent (`check` on a checked box). A refusal's `data.delivery` is `not_delivered` or `unknown`
  as for `act` (Delivery); the host retries on the same terms.
- `verified` is true when the tool read the element back and found what was asked.
- `post_state` `{role, states?, value?}` is that read.
- `surfaces` lists the kinds of overlay open after the action (`menu`, `sheet`, `alert`, `popover`):
  the program is waiting on one, which `ax.snapshot {surface}` reads.

An element action counts against the lease's actions and the rate like any other; the reads
(`ax.snapshot`, `ax.find`, `ax.get`) are not counted, as screenshots are not.

`1007 {reason: "needs_pointer" | "not_supported"}` is an element that offers no way to do this
without real input; the host falls back to a screenshot and `act`. The tool's real-input mode is
never used (below), so the daemon's own rules for physical input stay the only way pointer and keys
reach the desktop.

### The order of checks

A call is judged in this order, and the tool is not run until every step passes:

1. the parameters;
2. the lease: active, not expired, held by this `session` (`1200`);
3. the reference: handed out under this lease, recent, its window known (`1210`);
4. for `ax.act`, the pause for the operator's hand (`1101`);
5. the locked screen or secure desktop (`1204`), which also ends the lease;
6. the window, read fresh: on screen (`1210 window_gone` for a reference, `1001` for a `window`),
   not protected (`1203`), not elevated (`1207`), inside the lease's scope (`1202`). A covered or
   background window passes; a protected one never does, whatever covers what;
7. availability (`1209`), and the window mapped to the tool's (`1209 no_window`, `1211`);
8. for `ax.act`, the rate and the lease's `max_actions` (`1206`).

An `ax.act` holds the same input lock as `act`, so the two never interleave. A stop, the lease's end,
a locked screen or the operator's hand during it kills the child at once, and the refusal says
`delivery: "unknown"`, since the element may already have taken the action. A semantic action makes
no input events, so the watch for the operator's hand never takes it for the operator's.

**Blocked keys stay out of reach.** The tool's key command (`press`) is never run, and `type` and
`set_value` write text through the element, not as keystrokes, so no chord of Blocked keys can reach
the desktop through `ax.act`. A text with a line break is a line break in the field's value, not an
Enter.

### What the daemon lets the tool do

The tool can also drive real input (its `--headed` mode: synthetic clicks and keys, focusing windows)
and manage windows, the clipboard and apps. The daemon uses none of it:

- it runs only `version`, `list-windows` (Linux, to match windows), `snapshot`, `find`, `get`, `is`
  and the element actions of the table above
  (`click`, `set-value`, `clear`, `type`, `select`, `toggle`, `check`, `uncheck`, `expand`, `collapse`,
  `scroll`, `scroll-to`, `focus`), with arguments it builds itself; every value the host gave comes
  after `--`, so a text such as `--headed` is text;
- never `--headed`, `--force` or a pointer command, so where an element has no semantic path the tool
  refuses rather than falls back to input;
- the child's environment is the daemon's minus every `AGENT_DESKTOP_*` variable, plus
  `AGENT_DESKTOP_HOME`, the tool's state root: `<state-dir>/ax/<lease id>`, mode 0700, in the state
  directory the host never reads. Its reference maps hold the text of the screen, so it is deleted
  when the lease ends and emptied at start;
- the answer is read from stdout up to 8 MiB; a tree whose answer would pass 768 KiB is refused with
  `1003` (ask for `depth: "skeleton"` or a `root`). Its text is never logged.

On macOS the tool starts a helper process of its own (its command host), in a process group of its
own, that keeps the system's element handles between calls for up to 15 idle minutes. Killing a
call's process group does not reach it, so the daemon finds it by what it inherited from the call
(the tool's internal host marker and the lease's state root, which only the daemon sets) and kills
it when the lease ends, and every one left under the state directory when the daemon exits or
starts. Another program's copy of the tool is never touched.

### Windows

The daemon names windows in the system's ids; the tool in its own. Each snapshot binds the two once,
before the tool is asked for the tree, and the tool's answer must name the window it was asked for:

| System | The tool's window id | How the daemon maps its own |
|---|---|---|
| Windows | `w-<HWND in decimal>` | directly: `0x<hex>` is the same handle |
| macOS | `w-<window number>` | directly |
| Linux (X11) | `w-<AT-SPI bus>-<object path>`, not the X11 window | the tool's windows of the same process (`_NET_WM_PID`) whose bounds overlap the X11 window's by at least 60 % (intersection over union; the X11 frame includes the title bar, the AT-SPI frame does not); the best one, and `1211` when the two best are within 0.02 of each other. A window without bounds in the tree cannot be matched: `1209 no_window` |
| fake | `w-fake-<window id>` | as on Linux, so the tests exercise the hard case |

A surface (`menu`, `sheet`, `popover`, `alert`) may be its own window: on Windows and macOS its id
maps back directly and is judged like any window (step 6); on Linux it must be the window asked for,
or the snapshot is refused with `1202`.

*Not yet:* Wayland. AT-SPI works on any compositor, so the tree and the element actions could serve
a Wayland session without the X11 server; the lease's border, the operator's hand and the window
matching (no global window positions) need their own design first.

## Events

An `event` notification carries `{seq, at, type, data}`, one counter for all, as `ptyd`'s. The
daemon keeps the last 4096, no more than 8 MiB. Both roles may subscribe.

| Type | `data` |
|---|---|
| `lease.granted` | `{lease}` |
| `lease.ended` | `{lease_id, session, reason: "revoked" \| "expired" \| "stopped" \| "superseded" \| "locked", by: "host" \| "operator" \| "daemon", actions, secure?}`; `secure` is the 1204 reason when the lock ended it |
| `stopped` | `{by, lease_id, reason?, secure?}`; a lease ended by a locked screen sends it too, `by: "daemon"`, `reason: "locked"` |
| `human.input` | `{lease_id, how: "pointer" \| "input", human}` — the operator touched the machine; the agent is paused |
| `human.resumed` | `{by: "operator" \| "quiet", lease_id?}` |
| `permissions.changed` | `{permissions}` — read every second without a lease, every 2 s with one |
| `displays.changed` | `{displays}` — every screenshot id is now stale |

## A locked screen

A locked screen, a secure desktop (Windows' UAC prompt and Ctrl+Alt+Del screen) or a running screen
saver ends the lease the way `stop` does: the action in flight is cancelled at its next event,
everything held is released, the lease ends with the reason `locked`, and `lease.ended` and `stopped`
are published. Unlocking does not give the desktop back; the operator grants it again. The daemon
looks ten times a second while a lease is active, and again before every screenshot, action, focus
and preview, so the lease ends whether or not the agent is calling. The call that finds the lock is
refused with `1204` (`lease_ended: true`); the calls after it with `1200 {reason: "locked"}`, which
the host tells the model and the app shows beside the grant form. On X11 a screen locker shows as the
screen saver, so a screen saver that only blanks the screen ends the lease as well: the operator has
been away for its whole timeout, which is reason enough.

## The operator's hand

While a lease is active the daemon looks ten times a second for input it did not make, two ways:

- the pointer away from where the daemon's last action left it (by more than 2 units);
- the system's last-input time later than the daemon's own last input by more than 300 ms (X11's
  screen saver idle time, Windows' `GetLastInputInfo`, macOS's HID event state). This is what sees a
  key pressed with the pointer left alone.

Either pauses the agent at once and cancels the action in flight at its next event (`human.input`).
The pause ends with `resume`, or after `human_quiet_ms` with no further input (`human.resumed`).
The daemon does not look during the moment one of its own events is being sent; between the steps
of a drag, a typed text or a wait it does, and the action stops at its next step. Input within
300 ms of the daemon's own last event is taken for the daemon's.

*Not yet:* reading the operator's input directly, which would close both gaps — X11's XInput2 raw
events from devices other than XTest's, Windows' low-level hooks with the injected flag, a macOS
listen tap (it needs Input Monitoring).

## The audit

`audit.jsonl` holds one JSON object per line, metadata only: `{at, role, event: act | refused |
screenshot | focus | ax.snapshot | ax.find | ax.get | ax.act | lease.granted | lease.ended | stop |
resume | human.input, session, lease, action, code, reason, points (desktop units, rounded), button,
count, keys, keys_hidden, text_length, screenshot, ref, role_name, delivery, bytes, app, scope,
ttl_ms}`. `app` is a bundle id, executable or window class, never a title. An accessibility call
records its reference, the element's role (`role_name`: `button`, never its label) and the delivery,
never a name, a value or any text of the tree; a refused one has `event: refused` and the method in
`reason`. Typed text is its length only; a printable key is counted in `keys_hidden`,
never named, since a model can type a password one key at a time. No pixels. The file is rotated at
8 MiB into `audit.jsonl.1`.

## Permissions on macOS

`info.permissions` probes without asking: Screen Recording through
`CGPreflightScreenCaptureAccess`, Accessibility through `AXIsProcessTrusted`, Input Monitoring
through `CGPreflightListenEventAccess`. A capture without Screen Recording, and any input without
Accessibility, is refused with `1205` and a `hint` naming the setting. `permissions.request` is the
only call that may raise a system prompt, and only the operator's token reaches it.

The application is signed ad hoc for now, and macOS keys these grants to the exact build: after an
update System Settings may still show Daedalus switched on while the system has forgotten the grant.
The `hint` says so (remove Daedalus from the list and add it again), and the shell's permissions page
should repeat it when the probe says `denied` but the operator says it is on. `permissions.changed`
tells the shell when a grant arrives.

## Platforms

| | Linux X11 | Windows | macOS |
|---|---|---|---|
| Capture | `GetImage` on the root, in bands | GDI `BitBlt` from the screen | `CGDisplayCreateImageForRect` |
| Input | XTest; a character missing from the layout is bound to a spare keycode for the moment it is typed | `SendInput` with absolute virtual-desktop moves; `KEYEVENTF_UNICODE` typing | `CGEventPost` from a private event source; Unicode typing in chunks of 20 UTF-16 units |
| Displays | RandR monitors | `EnumDisplayMonitors`, per-monitor DPI | `CGGetActiveDisplayList`, backing scale |
| Windows, focus | EWMH (`_NET_CLIENT_LIST_STACKING`, `_NET_ACTIVE_WINDOW`), the window tree without a window manager | `EnumWindows`, cloaked and tool windows skipped | `CGWindowListCopyWindowInfo`, `NSRunningApplication` |
| Locked (ends the lease) | screen saver active | input desktop not `Default`, the session's lock flag, a running screen saver | the session dictionary's lock and console keys |
| Unreachable | — | a process at a higher integrity level, read from the token's integrity level before input is sent; `SendInput`'s count is a second check only, since it can report success while UIPI drops the events | — |
| Verified | by the tests and `deskd selftest` on Xvfb | compiled and vetted only | the pure helpers tested; the cgo parts not compiled here |

**Wayland** sessions are refused with a reason (`deskd serve` exits; `info` is never reached): the
X11 compatibility server would show the daemon only the programs still on X11. Wayland needs the
RemoteDesktop and ScreenCast portals (*not yet*).

## The fake desktop

`deskd serve --backend fake [--scene scene.json]` serves a desktop in memory: displays and windows
from the scene (the default is a Retina display with a 1080p one left of it, an editor, a terminal, a
window of an app the tests protect and an elevated program), a capture that paints each window in its
colour, and input recorded instead of sent. The host's tests run against it with no display.

```json
{"displays": [Display], "windows": [Window + {"color": [r, g, b], "ax"?: AXNode}], "focused": "<window id>"}
```

With `--ax-bin fake` the accessibility tree is served too, by a fake agent-desktop built into the
daemon (`deskd axfake`, run as the child like the real one): a window's `ax` is its tree in the
tool's own shape (`{role, name?, value?, states?, available_actions?, bounds?: {x, y, width,
height}, children?}`), and an element with any `available_actions` gets a reference. The default
scene gives the editor a small form (a text field, a password field, a check box, a Save button) and
the terminal a button. An element action is recorded among the `inputs` as `{kind: "ax", action, ref,
text?, window}` and changes nothing else.

`fake.simulate {human_input?, dx?, dy?, secure?, permissions?, move_window?, displays?, focus?}` →
`{inputs}` (operator only) changes the fake: the operator's hand (the pointer moves by `dx, dy` and
the last input is now), a locked screen, withdrawn grants, a moved window, new displays, the
keyboard on another window. `inputs` are the events recorded since the last call: `{kind: move |
button | scroll | key | type | ax, x, y, button, down, dx, dy, key, text, action, ref, window}`,
`window` being where the event landed.

## The selftest

`deskd selftest` checks the daemon on the machine it runs on, end to end through its own socket,
against a small window it opens and nothing else: the operator's lease is scoped to that window, so
the daemon's own rules refuse any point outside it. It checks the displays and permissions, that the
four coloured quadrants of the window are where the screenshot's coordinates put them, a click with
each button, a double click, typing through characters the layout lacks (`Hello, мир ✓ 😀`), chords,
the wheel, a drag, and the refusals: a point off the window, a screenshot of the whole display, a
screenshot id from before the window moved, anything after a stop. It exits 0 when every check
passed. On Linux without a display, or on a Wayland session, it starts a virtual display (Xvfb) of
its own. Touching the mouse during the run pauses it, and the report says so.

## For the host and the launcher

- **The launcher** starts `deskd serve --run-dir <local>/deskd/run --state-dir <local>/deskd/state
  --config <file>` as a child (like `browserd`), with Daedalus on the protected list, reads
  `operator.token`, connects with it, subscribes to events, and passes `COMPUTER_HOST_DIR` to the
  host. The shell's consent dialog becomes `lease.grant`, its Stop button and hotkey `stop`, its
  "give back" `resume`, its permissions page `info.permissions` and `permissions.request`. The
  shell's border, note and Stop shortcut follow `lease.status`, which the launcher asks once a second
  while the shell is attached (twice while a lease lasts) and passes on over their stdio link only
  when it changes, with the rectangles the lease covers in desktop units: the leased display's
  bounds, every display's for the whole desktop, from `info`; the leased window's, or every window
  of the leased app on screen, from `apps.list`. The shell converts them to its own units (on
  Windows with Electron's `screenToDipRect`, since each display has its own scale) and draws the
  border inside a display and just outside a window. The shell's Stop reaches the daemon the same
  way. The grant picker's thumbnails are `preview`, served by the launcher as
  `GET /api/computer/preview` with the JPEG's own bytes. It always passes `--ax-bin`: the path
  in `DAEDALUS_AGENT_DESKTOP`, else the `agent-desktop` file beside the launcher, else `off`.
- **The host** connects with the run directory's token, reads `info` (protocol, platform, units,
  displays, limits), and acts only while `lease.status` says its session holds the lease. It keeps
  the screenshot id with every picture it gives the model and passes it back with every point. It
  converts the model's coordinate convention (relative 0–1000, smart-resized pixels) to the
  screenshot's pixels before calling `act`; the daemon takes screenshot pixels only. It repeats an
  action by itself only as Delivery says. It offers the accessibility tree only when `info.ax.available`
  is true, passes references back exactly as `ax.snapshot` and `ax.find` gave them, fences the
  tree's text as untrusted as it does the screen's, and treats `1209` on a window as "use
  screenshots there".

## Not yet

The element under a point (`element.at`), the tree on Wayland, shell surfaces (task bar, tray, start
menu) in the tree, masking of secret fields and protected windows in screenshots, app tiers (view, click, full), the live view on channel
frames, Wayland through the portals, DXGI capture on Windows, ScreenCaptureKit on macOS, XShm on X11,
and the direct detectors of the operator's input above.
