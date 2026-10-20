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
| `lease.grant`, `resume`, `permissions.request` | no | yes |
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
| 1200 | `no_lease` | `{reason: "none" \| "expired" \| "stopped" \| "revoked" \| "other_session"}` |
| 1201 | `stale_screenshot` | the screenshot id cannot be used; `{screenshot_id, reason: "unknown" \| "other_lease" \| "old" \| "displays_changed" \| "window_moved" \| "window_gone"}`. Take a new screenshot |
| 1202 | `out_of_scope` | the point, the window or the display is outside the lease's scope, or the point is off the screenshot; `{scope, point?, window?}` |
| 1203 | `protected_target` | an app on the protected list (Daedalus itself); `{app, point?}` |
| 1204 | `secure_desktop` | a locked screen, a secure desktop or a running screen saver; `{reason: "lock_screen" \| "secure_desktop" \| "screen_saver"}` |
| 1205 | `no_permission` | the system has not granted what the call needs; `{which: "screen" \| "accessibility" \| "input_monitoring", hint}` |
| 1206 | `rate` | `{limit: "per_second", retry_after_ms}` or `{limit: "lease_actions", max}` |
| 1207 | `unreachable` | the system silently drops input to that window (Windows' UIPI in front of a program run as administrator); `{reason: "elevated", app?}` |
| 1208 | `operator_only` | the method needs the operator's token; `{role}` |

Every refusal happens before any input is sent, except the ones an action meets half way (a stop,
the lease's end, the operator's hand): those cut the action short at its next event and release
whatever it held down.

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

- **Display** `{id, name, bounds{x, y, w, h}, primary, pixels_per_unit, scale}`. `scale` is the
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
| `info` | → `{version, protocol, instance, role, os, arch, pid, started_at, uptime_s, platform{os, session_type: "x11" \| "win32" \| "quartz" \| "fake", units}, backend, displays: [Display], permissions, secure, lease: LeaseStatus, human: Human, limits{…}}`. It never raises a system prompt |
| `screenshot` | `{session, display? \| window?, max_edge? (64–8192, default 1568), format?: "jpeg" \| "png" (jpeg), quality? (1–100, 80)}` → `{screenshot_id, format, data (base64), width, height, display, window?, region, units_per_pixel[2], pixels_per_unit, scale, units, cursor?[2]}`. With neither `display` nor `window` the target is the lease's window or display, else the primary display. `cursor` is the pointer in picture pixels when it is on the picture. A JPEG too large for a frame steps its quality down; a PNG too large is `1003` (ask for JPEG or a smaller `max_edge`) |
| `act` | see Actions |
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
| `fake.simulate` | see The fake desktop |

## Actions

`act {session, action, …}` → `{action, points?: [[x, y]] (desktop units), target?: {window, app},
cursor[2], duration_ms, actions_left}`.

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
3. the pause for the operator's hand (`1101`);
4. the locked screen or secure desktop (`1204`);
5. the screenshot id and the conversion of every point (`1201`, `1202`);
6. where each point lands: the window under it, protected (`1203`), elevated (`1207`), outside the
   scope (`1202`);
7. for `type`, `key` and a click with modifiers, the window with the keyboard, judged the same way;
8. the rate (`actions_per_second`, `action_burst`) and the lease's `max_actions` (`1206`). `wait`
   is not counted.

## Events

An `event` notification carries `{seq, at, type, data}`, one counter for all, as `ptyd`'s. The
daemon keeps the last 4096, no more than 8 MiB. Both roles may subscribe.

| Type | `data` |
|---|---|
| `lease.granted` | `{lease}` |
| `lease.ended` | `{lease_id, session, reason: "revoked" \| "expired" \| "stopped" \| "superseded", by: "host" \| "operator" \| "daemon", actions}` |
| `stopped` | `{by, lease_id}` |
| `human.input` | `{lease_id, how: "pointer" \| "input", human}` — the operator touched the machine; the agent is paused |
| `human.resumed` | `{by: "operator" \| "quiet", lease_id?}` |
| `permissions.changed` | `{permissions}` — read every second without a lease, every 2 s with one |
| `displays.changed` | `{displays}` — every screenshot id is now stale |

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
screenshot | focus | lease.granted | lease.ended | stop | resume | human.input, session, lease,
action, code, reason, points (desktop units, rounded), button, count, keys, keys_hidden,
text_length, screenshot, bytes, app, scope, ttl_ms}`. `app` is a bundle id, executable or window
class, never a title. Typed text is its length only; a printable key is counted in `keys_hidden`,
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
| Locked | screen saver active | input desktop not `Default`, the session's lock flag, a running screen saver | the session dictionary's lock and console keys |
| Unreachable | — | a process at a higher integrity level | — |
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
{"displays": [Display], "windows": [Window + {"color": [r, g, b]}], "focused": "<window id>"}
```

`fake.simulate {human_input?, dx?, dy?, secure?, permissions?, move_window?, displays?, focus?}` →
`{inputs}` (operator only) changes the fake: the operator's hand (the pointer moves by `dx, dy` and
the last input is now), a locked screen, withdrawn grants, a moved window, new displays, the
keyboard on another window. `inputs` are the events recorded since the last call: `{kind: move |
button | scroll | key | type, x, y, button, down, dx, dy, key, text, window}`, `window` being where
the event landed.

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
  "give back" `resume`, its permissions page `info.permissions` and `permissions.request`; the
  overlay follows `lease.granted`, `lease.ended`, `human.input` and `stopped`.
- **The host** connects with the run directory's token, reads `info` (protocol, platform, units,
  displays, limits), and acts only while `lease.status` says its session holds the lease. It keeps
  the screenshot id with every picture it gives the model and passes it back with every point. It
  converts the model's coordinate convention (relative 0–1000, smart-resized pixels) to the
  screenshot's pixels before calling `act`; the daemon takes screenshot pixels only.

## Not yet

The accessibility tree and actions by reference (`ax.snapshot`, `element.at`), masking of secret
fields and protected windows in screenshots, app tiers (view, click, full), the live view on channel
frames, Wayland through the portals, DXGI capture on Windows, ScreenCaptureKit on macOS, XShm on X11,
and the direct detectors of the operator's input above.
