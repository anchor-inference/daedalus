// Package relay is the daemon's side of the operator's own Chrome: the Daedalus extension in it
// reaches the daemon over a relay (Chrome's native messaging through `browserd bridge`, or the
// host's WebSocket on a server) and the daemon drives the extension's tabs through it.
//
// The relay speaks JSON frames, each an object with a type in "t". The extension answers the
// daemon's DevTools commands (`cdp`) and browser-level operations (`call`) with `result`, forwards
// its tabs' DevTools events (`event`), and tells the daemon what the operator changed (`tabs`,
// `input`). The daemon checks that the relay is alive (`ping`) and confirms the popup's connection
// check (`check_ok`). testdata/frames.json holds one golden frame of every kind; the extension's
// tests read the same file.
package relay

import (
	"encoding/json"
	"fmt"
)

// Proto is the version of the relay protocol, sent in hello and welcome. It changes only when a
// frame changes meaning; on a mismatch both sides tell the operator which one to update rather
// than half understanding each other.
const Proto = 1

// The frame types, extension to daemon.
const (
	THello  = "hello"
	TResult = "result"
	TEvent  = "event"
	TTabs   = "tabs"
	TInput  = "input"
	TPong   = "pong"
	TCheck  = "check"
)

// The frame types, daemon to extension.
const (
	TWelcome = "welcome"
	TCDP     = "cdp"
	TCall    = "call"
	TPing    = "ping"
	TCheckOK = "check_ok"
	TBye     = "bye"
)

// The transports a relay arrives by.
const (
	TransportNative = "native"
	TransportServer = "server"
)

// The operations of a call frame: what the extension does with the ordinary extension APIs, since
// the debugger it holds reaches its tabs and nothing above them.
const (
	OpWindowEnsure    = "window.ensure"
	OpTabCreate       = "tab.create"
	OpTabClose        = "tab.close"
	OpTabActivate     = "tab.activate"
	OpWindowBounds    = "window.bounds"
	OpDownloadsSearch = "downloads.search"
	OpCookiesNames    = "cookies.names"
	OpGroupClose      = "group.close"
	OpNotify          = "notify"
)

// The ops of a tabs frame: what the operator changed about the agent's scope.
const (
	TabsCreated     = "created"
	TabsRemoved     = "removed"
	TabsUpdated     = "updated"
	TabsJoined      = "joined"
	TabsLeft        = "left"
	TabsGroupClosed = "group_closed"
)

// The reasons of a bye frame. The extension shows its own words for each; message is the daemon's,
// for a log.
const (
	ByeShutdown     = "shutdown"      // the daemon is stopping
	ByeReplaced     = "replaced"      // the same Chrome profile connected again, and this relay is the stale one
	ByeAnotherRelay = "another_relay" // another Chrome is connected; one relay at a time
	ByeProto        = "proto"         // the protocol versions differ
	ByeLost         = "lost"          // the relay stopped answering pings
	ByeBadFrame     = "bad_frame"     // a frame the daemon cannot read
)

// The codes of a result's error.
const (
	ErrRefused  = "refused"  // the allowlist, or a tab outside the agent's groups
	ErrNoTab    = "no_tab"   // the tab is gone, or was never the agent's
	ErrCDP      = "cdp"      // Chrome's own DevTools error; message is its text
	ErrDetached = "detached" // the debugger was detached from the tab (the operator, or Chrome)
	ErrFailed   = "failed"   // anything else
)

// Envelope is what every frame has: its type.
type Envelope struct {
	T string `json:"t"`
}

// Type reads the type of a frame without the rest of it.
func Type(b []byte) (string, error) {
	var e Envelope
	if err := json.Unmarshal(b, &e); err != nil {
		return "", fmt.Errorf("not a relay frame: %w", err)
	}
	if e.T == "" {
		return "", fmt.Errorf("a relay frame without its type")
	}
	return e.T, nil
}

// Hello is the extension's first frame.
type Hello struct {
	T             string `json:"t"`
	Proto         int    `json:"proto"`
	ExtVersion    string `json:"ext_version"`
	ChromeVersion string `json:"chrome_version"`
	Platform      string `json:"platform"`
	// ProfileID is a random id the extension keeps in chrome.storage.local: the same Chrome profile
	// reconnecting says the same one.
	ProfileID string `json:"profile_id"`
	// ProfileName is what the operator typed in the popup; "Chrome" unless they did.
	ProfileName string `json:"profile_name"`
	// Token is the device token, on the server transport only; the host checks it and the daemon
	// never sees one it has to judge.
	Token string `json:"token,omitempty"`
}

// Welcome is the daemon's answer to a hello it accepts.
type Welcome struct {
	T             string `json:"t"`
	Proto         int    `json:"proto"`
	DaemonVersion string `json:"daemon_version"`
	InstallName   string `json:"install_name"`
	InstallKind   string `json:"install_kind"`
}

// CDP is one DevTools command to a debuggee. Session is "x<tabId>" for a tab, or
// "x<tabId>/<sessionId>" for a flat child session of it (a frame of another site); Params are
// passed to chrome.debugger.sendCommand as they are.
type CDP struct {
	T       string          `json:"t"`
	ID      int64           `json:"id"`
	Session string          `json:"session"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Call is one browser-level operation.
type Call struct {
	T    string `json:"t"`
	ID   int64  `json:"id"`
	Op   string `json:"op"`
	Args any    `json:"args"`
}

// Result answers a cdp or a call frame by its id.
type Result struct {
	T     string          `json:"t"`
	ID    int64           `json:"id"`
	OK    bool            `json:"ok"`
	Value json.RawMessage `json:"value,omitempty"`
	Error *Error          `json:"error,omitempty"`
}

// Error is a result's failure.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string {
	if e.Message == "" {
		return "the relay answered " + e.Code
	}
	return e.Message + " (" + e.Code + ")"
}

// Event is one DevTools event of a debuggee. Params are as Chrome sent them: the daemon, not the
// extension, turns a child session id inside Target.attachedToTarget into "x<tabId>/<sessionId>".
type Event struct {
	T       string          `json:"t"`
	Session string          `json:"session"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Tab is a tab as the extension describes it. ID is Chrome's tab id; Group the daemon group whose
// Chrome tab group holds it; Opener the tab that opened it, when that is one of the agent's.
type Tab struct {
	ID     int    `json:"id"`
	Group  string `json:"group,omitempty"`
	Opener int    `json:"opener,omitempty"`
	URL    string `json:"url,omitempty"`
	Title  string `json:"title,omitempty"`
}

// Tabs is a change of scope the operator made.
type Tabs struct {
	T     string `json:"t"`
	Op    string `json:"op"`
	Tab   *Tab   `json:"tab,omitempty"`
	Group string `json:"group,omitempty"`
}

// Input is the operator's own trusted input in an agent tab: control goes to the person.
type Input struct {
	T   string `json:"t"`
	Tab int    `json:"tab"`
}

// Ping, Pong and Check carry a counter; CheckOK confirms a check.
type Ping struct {
	T string `json:"t"`
	N int64  `json:"n"`
}

type Pong = Ping

type Check = Ping

type CheckOK struct {
	T        string `json:"t"`
	N        int64  `json:"n"`
	HostSeen bool   `json:"host_seen"`
}

// Bye ends a relay, from the daemon's side.
type Bye struct {
	T       string `json:"t"`
	Reason  string `json:"reason"`
	Message string `json:"message,omitempty"`
	// Proto is the daemon's protocol, on a bye for a mismatch, so the extension can say which side
	// to update.
	Proto int `json:"proto,omitempty"`
}

// The arguments and answers of the call operations.

// GroupArgs names a daemon group: window.ensure, group.close.
type GroupArgs struct {
	Group string `json:"group"`
}

// WindowEnsured is window.ensure's answer: the agent's own window for the group, and the tabs its
// Chrome tab group already holds (a group that outlived a relay that dropped), which the daemon
// takes up again.
type WindowEnsured struct {
	Window int   `json:"window"`
	Tabs   []Tab `json:"tabs"`
}

// TabCreateArgs is tab.create's; its answer is TabCreated.
type TabCreateArgs struct {
	Group string `json:"group"`
	URL   string `json:"url"`
}

type TabCreated struct {
	Tab Tab `json:"tab"`
}

// TabArgs names a tab: tab.close, tab.activate.
type TabArgs struct {
	Tab int `json:"tab"`
}

// Bounds are a window's, in screen pixels.
type Bounds struct {
	Left   int `json:"left"`
	Top    int `json:"top"`
	Width  int `json:"width"`
	Height int `json:"height"`
}

// WindowBoundsArgs is window.bounds's: with Bounds the window is given that size (only width and
// height are set), without it the bounds are read. Either way the answer is WindowBounds.
type WindowBoundsArgs struct {
	Group  string `json:"group"`
	Bounds *Size  `json:"bounds,omitempty"`
}

type Size struct {
	Width  int `json:"width"`
	Height int `json:"height"`
}

type WindowBounds struct {
	Bounds Bounds `json:"bounds"`
}

// DownloadsSearchArgs is downloads.search's: ID is the DevTools guid of the download, as
// Page.downloadWillBegin named it; the extension finds the chrome.downloads item behind it. The
// answer is Download.
type DownloadsSearchArgs struct {
	ID string `json:"id"`
}

type Download struct {
	Path  string `json:"path"`
	State string `json:"state"`
	Bytes int64  `json:"bytes"`
}

// CookiesNamesArgs is cookies.names's: the cookies Chrome would send to URL. The answer is
// CookieNames, every field of each cookie but its value, which never leaves the extension.
type CookiesNamesArgs struct {
	URL string `json:"url"`
}

type CookieName struct {
	Name     string  `json:"name"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	Secure   bool    `json:"secure"`
	HTTPOnly bool    `json:"http_only"`
	SameSite string  `json:"same_site,omitempty"`
	Session  bool    `json:"session"`
}

type CookieNames struct {
	Cookies []CookieName `json:"cookies"`
}

// NotifyArgs is notify's: a short line the popup shows, with a flash of the badge.
type NotifyArgs struct {
	Text string `json:"text"`
}
