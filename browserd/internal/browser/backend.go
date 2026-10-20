package browser

import (
	"context"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
)

// The two ways the daemon has a browser. Everything the manager does above a page — starting the
// browser, making and closing tabs, sizing windows, the browser's downloads — goes through a
// Backend; what it does on a page goes through the page's session on the browser's *cdp.Conn
// either way, so the page, view and workflow packages do not know which backend they run on.
const (
	// BackendLaunch starts Chromium on the agent's own profiles and owns it whole.
	BackendLaunch = config.BackendLaunch
	// BackendAttach drives the operator's own Chrome through the Daedalus extension over a relay
	// (internal/relay): only the tabs of the agent's groups, never the browser around them.
	BackendAttach = config.BackendAttach
)

// UserProfile is the one browser of the attach backend: the operator's Chrome, whatever profile the
// host names, since the daemon has no profiles of its own to give it.
const UserProfile = "user"

// Backend is how the manager reaches a browser above its pages. Its methods are called with the
// manager's lock free.
type Backend interface {
	// Name is BackendLaunch or BackendAttach, as daemon.info reports it.
	Name() string

	// start brings up the browser of profile, ready for its first group.
	start(ctx context.Context, profile string) (*Browser, error)
	// openGroup prepares a new group's place in its browser: a throwaway context, the agent's own
	// window.
	openGroup(ctx context.Context, g *Group) error
	// closeGroup closes a group's tabs and whatever openGroup made for it.
	closeGroup(ctx context.Context, g *Group, tabs []*Tab)
	// newTab opens a tab in the group, prepared and published.
	newTab(ctx context.Context, g *Group) (*Tab, error)
	// closeTab closes one tab and returns once it is gone.
	closeTab(ctx context.Context, t *Tab) error
	// closeRefused closes a page that would put its group past its tab cap: a popup.
	closeRefused(ctx context.Context, b *Browser, target, session string)
	// activate brings a tab forward.
	activate(ctx context.Context, t *Tab) error
	// refreshTab reads a tab's address and title afresh; refresh does every tab of a browser.
	refreshTab(ctx context.Context, t *Tab)
	refresh(ctx context.Context, b *Browser)
	// windowSize is the size of the window a page of the group is in; setWindowSize changes it.
	windowSize(ctx context.Context, g *Group, target string) (w, h int, err error)
	setWindowSize(ctx context.Context, g *Group, target string, w, h int) error
	// cancelDownload stops a download of the browser's, by its DevTools guid.
	cancelDownload(ctx context.Context, b *Browser, guid string) error
	// downloadPath is where the browser put a finished download, and whether that file is the
	// daemon's to move (its own download directory) or only to copy (the operator's).
	downloadPath(ctx context.Context, b *Browser, guid string) (path string, own bool, err error)
}

// process is a running browser as the manager waits for it and ends it: Chromium's process tree,
// or a relayed Chrome the daemon only detaches from.
type process interface {
	Done() <-chan struct{}
	Close(grace time.Duration)
	Kill()
	ExitCode() int
}

// Backend is the manager's backend.
func (m *Manager) Backend() Backend { return m.backend }

// Attached reports whether the manager drives the operator's Chrome rather than browsers of its own.
func (m *Manager) Attached() bool { return m.backend.Name() == BackendAttach }

// Attached reports whether the browser is the operator's Chrome behind a relay.
func (b *Browser) Attached() bool { return b.attached }

// CancelDownload stops one of the browser's downloads.
func (b *Browser) CancelDownload(ctx context.Context, guid string) error {
	return b.m.backend.cancelDownload(ctx, b, guid)
}

// DownloadPath is where a finished download of the browser is, and whether the daemon may move it.
func (b *Browser) DownloadPath(ctx context.Context, guid string) (string, bool, error) {
	return b.m.backend.downloadPath(ctx, b, guid)
}
