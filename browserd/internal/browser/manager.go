// Package browser is the daemon's model: browsers (one Chromium on one profile), the groups of tabs
// each agent owner has in one, the tabs, and who controls a group. It starts and ends browsers,
// follows their targets, and publishes what happens. What a page looks like and how it is acted on
// are the page package's; how it is watched is the view package's.
package browser

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/cdp"
	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/display"
	"github.com/ascorblack/daedalus/browserd/internal/relay"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// EphemeralProfile is the reserved profile of throwaway contexts.
const EphemeralProfile = "ephemeral"

var idPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// ValidID reports whether s may be a group or profile id.
func ValidID(s string) bool { return idPattern.MatchString(s) }

// Listener is told what happens on tabs, so the view and page packages can follow them without the
// model knowing about either. Calls come from a browser's event goroutine and must not block.
type Listener interface {
	TabEvent(t *Tab, e cdp.Event)
	BrowserEvent(b *Browser, e cdp.Event)
	TabGone(t *Tab)
}

// Deps are what the manager needs from the daemon.
type Deps struct {
	Config *config.Config
	Log    *slog.Logger
	Events *events.Debouncer
	// Wall is the network wall (internal/netwall): a proxy for each browser, and the check every
	// navigation passes. Nil starts browsers without one, which only a test does.
	Wall Wall
	// Busy says whether someone watches the browser, which keeps it from closing as idle.
	Busy func(b *Browser) bool
	// Relay is where the operator's Chrome connects; the attach backend (config.Backend) drives the
	// browser behind it, and needs it.
	Relay *relay.Hub
}

// Wall is the browsers' one way out. Open gives a browser about to start the address of its own
// proxy, and Close ends that proxy once the browser has exited; Navigation judges a top-level
// navigation before it is made, and its error (1102, the verdict in its data) is the caller's
// answer as it stands.
type Wall interface {
	Open(browserID string) (addr string, err error)
	// Track keeps the grants of a browser that has no proxy of its own: the operator's Chrome, whose
	// pages leave from the operator's machine. Close ends them with the browser.
	Track(browserID string)
	Close(browserID string)
	Navigation(ctx context.Context, browserID, url string) error
	// Allowlist reports whether the operator has an allowlist, which a page's own navigations are
	// then judged by too (guard.go).
	Allowlist() bool
}

// Manager holds every browser, group and tab of the daemon.
type Manager struct {
	deps    Deps
	lim     config.Limits
	log     *slog.Logger
	backend Backend

	mu        sync.Mutex
	browsers  map[string]*Browser // by id
	byProfile map[string]*Browser
	starting  map[string]chan struct{} // profiles whose browser is starting
	groups    map[string]*Group
	tabs      map[string]*Tab
	bySession map[string]*Tab
	settingUp map[string]*Tab // pages being set up, by session: their first navigation may pause already
	byTarget  map[string]*Tab
	// The sessions of the frames of other sites in a tab (each runs in a process of its own and is
	// attached to on its own), to the tab they are in.
	frameTabs map[string]*Tab
	gone      map[string]goneGroup // groups whose browser went away, for an hour, to answer 1108
	listeners []Listener
	tabSeq    int
	found     chrome.Found
	foundOK   bool
	probed    string // the version the found Chromium printed, before any browser ran
	sandbox   string
	closing   bool
	// The virtual display headed browsers draw on when the daemon has none; started with the first
	// such browser, and again if it ever exits.
	display *display.Display

	// The sessions of the agent's extensions' workers, followed for their console (workers.go).
	extWorkers map[string]*extWorker
}

type goneGroup struct {
	reason string
	at     time.Time
}

// New returns a manager. It starts nothing until a browser is opened.
func New(deps Deps) *Manager {
	m := &Manager{deps: deps, lim: deps.Config.Limits, log: deps.Log,
		browsers: map[string]*Browser{}, byProfile: map[string]*Browser{}, starting: map[string]chan struct{}{},
		groups: map[string]*Group{}, tabs: map[string]*Tab{}, bySession: map[string]*Tab{}, settingUp: map[string]*Tab{}, byTarget: map[string]*Tab{},
		frameTabs: map[string]*Tab{}, gone: map[string]goneGroup{}, sandbox: "unknown"}
	if deps.Config.Chromium.NoSandbox {
		m.sandbox = "off by configuration"
	}
	if deps.Config.Backend == BackendAttach {
		// The operator's Chrome is the browser: the daemon neither looks for a Chromium of its own
		// nor runs one to ask its version.
		m.backend = &attach{m: m, hub: deps.Relay, dispatched: map[int]time.Time{}}
		return m
	}
	m.backend = &launch{m: m}
	m.found, m.foundOK = chrome.Find(deps.Config.Chromium.Path)
	if m.foundOK {
		go m.probeVersion(m.found.Path)
	}
	return m
}

// probeVersion learns the found Chromium's version before any browser runs.
func (m *Manager) probeVersion(path string) {
	v := chrome.ProbeVersion(path, 10*time.Second)
	m.mu.Lock()
	if m.found.Path == path {
		m.probed = v
	}
	m.mu.Unlock()
}

// ChromiumVersion is the version of the Chromium the daemon runs: a running browser's own word, else
// what the executable printed, else "".
func (m *Manager) ChromiumVersion() string {
	for _, b := range m.Browsers() {
		if b.Version != "" {
			return b.Version
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.probed
}

// Listen adds a listener. It is called before the daemon serves anyone.
func (m *Manager) Listen(l Listener) { m.listeners = append(m.listeners, l) }

// Limits are the limits the manager enforces.
func (m *Manager) Limits() config.Limits {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lim
}

// LimitsUpdate is what limits.set may change while the daemon runs: the host's settings, sent on
// every connection, so the operator's cap and idle time are the daemon's without a restart. Nil
// leaves a limit as it is.
type LimitsUpdate struct {
	MaxBrowsers       *int   `json:"max_browsers,omitempty"`
	IdleCloseMs       *int64 `json:"idle_close_ms,omitempty"`
	RecordMaxBytes    *int64 `json:"record_max_bytes,omitempty"`
	RecordRetentionMs *int64 `json:"record_retention_ms,omitempty"`
}

// SetLimits applies u. A lower cap closes no running browser: it only refuses the next one.
func (m *Manager) SetLimits(u LimitsUpdate) (config.Limits, error) {
	if u.MaxBrowsers != nil && (*u.MaxBrowsers < 1 || *u.MaxBrowsers > 64) {
		return config.Limits{}, wire.Errorf(wire.CodeInvalidParams, "max_browsers must be 1-64")
	}
	if u.IdleCloseMs != nil && (*u.IdleCloseMs < 0 || *u.IdleCloseMs > 7*24*3600*1000) {
		return config.Limits{}, wire.Errorf(wire.CodeInvalidParams, "idle_close_ms must be 0-604800000")
	}
	if u.RecordMaxBytes != nil && (*u.RecordMaxBytes < 1<<20 || *u.RecordMaxBytes > 1<<40) {
		return config.Limits{}, wire.Errorf(wire.CodeInvalidParams, "record_max_bytes must be 1 MiB-1 TiB")
	}
	if u.RecordRetentionMs != nil && (*u.RecordRetentionMs < 3600*1000 || *u.RecordRetentionMs > 366*24*3600*1000) {
		return config.Limits{}, wire.Errorf(wire.CodeInvalidParams, "record_retention_ms must be an hour to a year")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if u.MaxBrowsers != nil {
		m.lim.MaxBrowsers = *u.MaxBrowsers
	}
	if u.IdleCloseMs != nil {
		m.lim.IdleCloseMs = *u.IdleCloseMs
		m.lim.IdleClose = time.Duration(*u.IdleCloseMs) * time.Millisecond
	}
	if u.RecordMaxBytes != nil {
		m.lim.RecordMaxBytes = *u.RecordMaxBytes
	}
	if u.RecordRetentionMs != nil {
		m.lim.RecordRetentionMs = *u.RecordRetentionMs
	}
	return m.lim, nil
}

// Chromium is the browser the daemon runs, and "" with the reason when there is none.
func (m *Manager) Chromium() (chrome.Found, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.found, m.foundOK
}

// Sandbox is the state of Chromium's sandbox: "ok", "unknown" before a browser ran, or why not.
func (m *Manager) Sandbox() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sandbox
}

func (m *Manager) publish(typ string, data any) {
	if m.deps.Events != nil {
		m.deps.Events.Publish(typ, "", data)
	}
}

// Publish publishes a daemon event (the page model's actions, downloads and requests for the
// operator).
func (m *Manager) Publish(typ string, data any) { m.publish(typ, data) }

// publishKeyed goes through the rate limit of its type, per key (a tab).
func (m *Manager) publishKeyed(typ, key string, data any) {
	if m.deps.Events != nil {
		m.deps.Events.Publish(typ, key, data)
	}
}

// Counts is what daemon.info reports.
func (m *Manager) Counts() map[string]int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return map[string]int{"browsers": len(m.browsers), "groups": len(m.groups), "tabs": len(m.tabs)}
}

// Group returns the group with id, or the error an agent should read: 1108 when its browser went
// away, 1001 when there never was one.
func (m *Manager) Group(id string) (*Group, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if g := m.groups[id]; g != nil {
		return g, nil
	}
	if gg, ok := m.gone[id]; ok {
		return nil, errBrowserGone(gg.reason)
	}
	return nil, wire.Errorf(wire.CodeNotFound, "no browser group %q; open one", id)
}

// TabByTarget returns the tab of a CDP target (a page's main frame has the target's id), or nil.
func (m *Manager) TabByTarget(target string) *Tab {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.byTarget[target]
}

// Tab returns the tab with id.
func (m *Manager) Tab(id string) (*Tab, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if t := m.tabs[id]; t != nil {
		return t, nil
	}
	return nil, errNoSuchTab(id)
}

// Browsers lists the running browsers, oldest first.
func (m *Manager) Browsers() []*Browser {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]*Browser, 0, len(m.browsers))
	for _, b := range m.browsers {
		out = append(out, b)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt.Before(out[j].StartedAt) })
	return out
}

// Groups lists the groups, of one browser when browserID is set.
func (m *Manager) Groups(browserID string) []*Group {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []*Group
	for _, g := range m.groups {
		if browserID == "" || g.Browser.ID == browserID {
			out = append(out, g)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out
}

// OpenParams are browser.open's.
type OpenParams struct {
	GroupID  string            `json:"group_id"`
	Profile  string            `json:"profile"`
	Labels   map[string]string `json:"labels,omitempty"`
	Viewport *Viewport         `json:"viewport,omitempty"`
	URL      string            `json:"url,omitempty"`
}

// Viewport is a page's size in CSS pixels.
type Viewport struct {
	W int `json:"w"`
	H int `json:"h"`
}

// Open opens a group, starting its profile's browser when needed, with a first tab.
func (m *Manager) Open(ctx context.Context, p OpenParams) (*Group, *Tab, bool, error) {
	if !ValidID(p.GroupID) {
		return nil, nil, false, wire.Errorf(wire.CodeInvalidParams, "group_id must be 1-64 of A-Z a-z 0-9 - _")
	}
	if !ValidID(p.Profile) {
		return nil, nil, false, wire.Errorf(wire.CodeInvalidParams, "profile must be 1-64 of A-Z a-z 0-9 - _")
	}
	if err := checkLabels(p.Labels); err != nil {
		return nil, nil, false, err
	}
	if m.Attached() && p.Profile == EphemeralProfile {
		return nil, nil, false, ErrAttachRefused("a throwaway context")
	}
	vp := Viewport{W: config.DefaultViewportW, H: config.DefaultViewportH}
	if p.Viewport != nil {
		vp = *p.Viewport
		if vp.W < config.MinViewport || vp.W > config.MaxViewport || vp.H < config.MinViewport || vp.H > config.MaxViewport {
			return nil, nil, false, wire.Errorf(wire.CodeInvalidParams, "viewport sides must be %d-%d", config.MinViewport, config.MaxViewport)
		}
	}
	if p.URL != "" {
		if err := CheckURL(p.URL, ActorAgent); err != nil {
			return nil, nil, false, err
		}
	}
	m.mu.Lock()
	if g := m.groups[p.GroupID]; g != nil {
		m.mu.Unlock()
		if g.Profile != p.Profile {
			return nil, nil, false, wire.Errorf(wire.CodeForbidden, "group %q is open on profile %q, not %q", g.ID, g.Profile, p.Profile)
		}
		return g, g.ActiveTab(), false, nil
	}
	m.mu.Unlock()
	key := p.Profile
	if m.Attached() {
		// One browser, the operator's Chrome, whatever profile the host keeps its groups under.
		key = UserProfile
	}
	b, err := m.ensureBrowser(ctx, key)
	if err != nil {
		return nil, nil, false, err
	}
	g, err := m.newGroup(ctx, b, p, vp)
	if err != nil {
		return nil, nil, false, err
	}
	var t *Tab
	if m.Attached() {
		// A group taken up again in Chrome has its tabs already.
		t = g.ActiveTab()
	}
	if t == nil {
		if t, err = m.NewTab(ctx, g, "", false); err != nil {
			m.CloseGroup(context.Background(), g.ID)
			return nil, nil, false, err
		}
	}
	m.publish("group.opened", map[string]any{"group_id": g.ID, "browser_id": b.ID, "profile": g.Profile, "labels": g.Labels})
	if p.URL != "" {
		// A navigation that fails still leaves the group open: the agent reads the error and
		// decides, as it would for any page.
		if _, err := m.Navigate(ctx, t, p.URL, 30*time.Second); err != nil {
			m.log.Info("first navigation failed", "group", g.ID, "error", err.Error())
		}
	}
	return g, t, true, nil
}

func checkLabels(labels map[string]string) error {
	if len(labels) > 32 {
		return wire.Errorf(wire.CodeInvalidParams, "at most 32 labels")
	}
	for k, v := range labels {
		if len(k) > 256 || len(v) > 256 {
			return wire.Errorf(wire.CodeInvalidParams, "a label is at most 256 bytes")
		}
	}
	return nil
}

// ensureBrowser returns the running browser of profile, starting it when there is none. The
// ephemeral profile's browser is shared by every throwaway group.
func (m *Manager) ensureBrowser(ctx context.Context, profile string) (*Browser, error) {
	for {
		m.mu.Lock()
		if m.closing {
			m.mu.Unlock()
			return nil, wire.Errorf(wire.CodeUnsupported, "the daemon is stopping")
		}
		if b := m.byProfile[profile]; b != nil {
			m.mu.Unlock()
			return b, nil
		}
		if wait := m.starting[profile]; wait != nil {
			m.mu.Unlock()
			select {
			case <-wait:
				continue
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		if n := len(m.browsers) + len(m.starting); n >= m.lim.MaxBrowsers {
			m.mu.Unlock()
			return nil, errLimit("browsers", m.lim.MaxBrowsers, "%d browsers are running, the most this daemon runs at once; close one first", n)
		}
		done := make(chan struct{})
		m.starting[profile] = done
		m.mu.Unlock()
		b, err := m.backend.start(ctx, profile)
		m.mu.Lock()
		delete(m.starting, profile)
		if err == nil {
			m.browsers[b.ID] = b
			m.byProfile[profile] = b
		}
		m.mu.Unlock()
		close(done)
		if err != nil {
			return nil, err
		}
		m.publish("browser.started", map[string]any{"browser_id": b.ID, "profile": profile, "pid": b.pid,
			"chromium_version": b.Version})
		go m.watch(b)
		if !b.attached {
			go m.probeSandbox(b)
		}
		return b, nil
	}
}

func newID(prefix string) string {
	raw := make([]byte, 4)
	_, _ = rand.Read(raw)
	return prefix + hex.EncodeToString(raw)
}

// ProfileDir is where a persistent profile lives.
func (m *Manager) ProfileDir(profile string) string {
	return filepath.Join(m.deps.Config.StateDir, "profiles", profile)
}

// newBrowser is a browser not yet started, with the bookkeeping every backend's needs.
func (m *Manager) newBrowser(profile string) *Browser {
	return &Browser{ID: newID("b"), Profile: profile, StartedAt: time.Now().UTC(),
		groups: map[string]*Group{}, contexts: map[string]*Group{}, claims: map[string]chan *Tab{},
		claimGroups: map[string]*Group{}, early: map[string]pageAttach{}, gone: make(chan struct{}), m: m,
		screenKnown: make(chan struct{})}
}

// browserEnv is the environment a browser starts with: the daemon's own, and for a headed browser
// where the daemon has no display, the virtual one, started if it is not running. nil inherits.
func (m *Manager) browserEnv() ([]string, error) {
	if !m.deps.Config.Chromium.Headed() || !needsX() || os.Getenv("DISPLAY") != "" {
		return nil, nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.display == nil || !m.display.Alive() {
		w, h := m.deps.Config.Chromium.Screen()
		d, err := display.Start(w, h)
		if err != nil {
			return nil, err
		}
		m.log.Info("virtual display started", "display", d.Name, "screen", fmt.Sprintf("%dx%d", w, h))
		m.display = d
	}
	env := []string{"DISPLAY=" + m.display.Name}
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "DISPLAY=") {
			env = append(env, kv)
		}
	}
	return env, nil
}

// needsX says whether a window here is an X window: on macOS and Windows a headed browser opens
// the system's own windows and no display is started for it.
func needsX() bool { return runtime.GOOS != "darwin" && runtime.GOOS != "windows" }

// closeWall ends the proxy of a browser that exited or never started.
func (m *Manager) closeWall(b *Browser) {
	if m.deps.Wall != nil {
		m.deps.Wall.Close(b.ID)
	}
}

func lastLine(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.LastIndexByte(s, '\n'); i >= 0 {
		s = s[i+1:]
	}
	if len(s) > 300 {
		s = s[:300]
	}
	return s
}

// userAgent is the browser's user agent without the word Headless, and the client hints that match
// it. It is what the same browser says with a window, not a disguise.
func userAgent(product, ua string) (string, map[string]any) {
	ua = strings.ReplaceAll(ua, "HeadlessChrome/", "Chrome/")
	version := product
	if i := strings.IndexByte(product, '/'); i >= 0 {
		version = product[i+1:]
	}
	major := version
	if i := strings.IndexByte(version, '.'); i >= 0 {
		major = version[:i]
	}
	platform := "Linux"
	switch {
	case strings.Contains(ua, "Windows"):
		platform = "Windows"
	case strings.Contains(ua, "Macintosh"):
		platform = "macOS"
	}
	brands := []map[string]string{{"brand": "Chromium", "version": major}, {"brand": "Not.A/Brand", "version": "99"}}
	full := []map[string]string{{"brand": "Chromium", "version": version}, {"brand": "Not.A/Brand", "version": "99.0.0.0"}}
	return ua, map[string]any{"brands": brands, "fullVersionList": full, "fullVersion": version, "platform": platform,
		"platformVersion": "", "architecture": "", "model": "", "mobile": false}
}

func (m *Manager) newGroup(ctx context.Context, b *Browser, p OpenParams, vp Viewport) (*Group, error) {
	m.mu.Lock()
	if g := m.groups[p.GroupID]; g != nil {
		m.mu.Unlock()
		return g, nil
	}
	if len(b.groups) >= m.lim.MaxGroupsPerBrowser {
		m.mu.Unlock()
		return nil, errLimit("groups", m.lim.MaxGroupsPerBrowser, "this browser holds %d groups, the most it may", len(b.groups))
	}
	labels := p.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	now := time.Now().UTC()
	g := &Group{ID: p.GroupID, Browser: b, Profile: p.Profile, Viewport: vp, Labels: labels, CreatedAt: now,
		lastActivity: now, control: Control{Owner: OwnerAgent}, wake: make(chan struct{}), m: m}
	// Reserved before the context exists, so a second open of the same group waits for this one.
	m.groups[g.ID] = g
	b.groups[g.ID] = g
	delete(m.gone, g.ID)
	m.mu.Unlock()
	if err := m.backend.openGroup(ctx, g); err != nil {
		m.mu.Lock()
		delete(m.groups, g.ID)
		delete(b.groups, g.ID)
		m.mu.Unlock()
		return nil, err
	}
	if g.ContextID != "" {
		m.mu.Lock()
		b.contexts[g.ContextID] = g
		m.mu.Unlock()
	}
	return g, nil
}

// CloseGroup closes a group's tabs and forgets it. A throwaway group's context goes with it.
func (m *Manager) CloseGroup(ctx context.Context, id string) (int, error) {
	g, err := m.Group(id)
	if err != nil {
		return 0, err
	}
	tabs := g.Tabs()
	m.backend.closeGroup(ctx, g, tabs)
	m.forgetGroup(g, tabs)
	m.publish("group.closed", map[string]any{"group_id": g.ID, "browser_id": g.Browser.ID, "profile": g.Profile, "labels": g.Labels})
	return len(tabs), nil
}

// forgetGroup drops a closed group and its tabs from the model.
func (m *Manager) forgetGroup(g *Group, tabs []*Tab) {
	m.mu.Lock()
	delete(m.groups, g.ID)
	delete(g.Browser.groups, g.ID)
	delete(g.Browser.contexts, g.ContextID)
	for _, t := range tabs {
		m.forgetTabLocked(t)
	}
	m.mu.Unlock()
	for _, t := range tabs {
		m.tabGone(t)
	}
	g.closeControl()
}

// CloseBrowser ends a browser and forgets its groups.
func (m *Manager) CloseBrowser(id string, reason string) ([]string, error) {
	m.mu.Lock()
	b := m.browsers[id]
	m.mu.Unlock()
	if b == nil {
		return nil, wire.Errorf(wire.CodeNotFound, "no browser %q", id)
	}
	groups := b.GroupIDs()
	b.setClosing(reason)
	b.proc.Close(config.CloseGrace)
	<-b.gone
	return groups, nil
}

// watch waits for a browser to exit and forgets it, publishing why.
func (m *Manager) watch(b *Browser) {
	<-b.proc.Done()
	reason, crashed := b.closeReason()
	m.mu.Lock()
	delete(m.browsers, b.ID)
	if m.byProfile[b.Profile] == b {
		delete(m.byProfile, b.Profile)
	}
	var groups []string
	var tabs []*Tab
	for id, g := range b.groups {
		groups = append(groups, id)
		delete(m.groups, id)
		m.gone[id] = goneGroup{reason: reason, at: time.Now()}
		for _, t := range g.tabs {
			tabs = append(tabs, t)
			m.forgetTabLocked(t)
		}
	}
	for _, t := range m.tabs {
		if t.Group != nil && t.Group.Browser == b {
			tabs = append(tabs, t)
			m.forgetTabLocked(t)
		}
	}
	m.mu.Unlock()
	for _, t := range tabs {
		m.tabGone(t)
	}
	for _, g := range b.groupList() {
		g.closeControl()
	}
	m.forgetWorkersOf(b)
	b.removeTemp()
	m.closeWall(b)
	sort.Strings(groups)
	m.log.Info("browser exited", "browser", b.ID, "reason", reason, "code", b.proc.ExitCode())
	m.publish("browser.exited", map[string]any{"browser_id": b.ID, "profile": b.Profile, "code": b.proc.ExitCode(),
		"crashed": crashed, "reason": reason, "groups": groups})
	close(b.gone)
}

func (m *Manager) forgetTabLocked(t *Tab) {
	delete(m.tabs, t.ID)
	delete(m.bySession, t.Session)
	delete(m.byTarget, t.TargetID)
	for _, s := range t.frameSessions() {
		delete(m.frameTabs, s)
	}
	if g := t.Group; g != nil {
		for i, x := range g.tabs {
			if x == t {
				g.tabs = append(g.tabs[:i], g.tabs[i+1:]...)
				break
			}
		}
		if g.active == t {
			g.active = nil
			if len(g.tabs) > 0 {
				g.active = g.tabs[len(g.tabs)-1]
			}
		}
	}
}

func (m *Manager) tabGone(t *Tab) {
	t.markClosed()
	for _, l := range m.listeners {
		l.TabGone(t)
	}
}

// Shutdown closes every browser, in parallel, each with its grace.
func (m *Manager) Shutdown() {
	m.mu.Lock()
	m.closing = true
	bs := make([]*Browser, 0, len(m.browsers))
	for _, b := range m.browsers {
		bs = append(bs, b)
	}
	m.mu.Unlock()
	var wg sync.WaitGroup
	for _, b := range bs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			b.setClosing("shutdown")
			b.proc.Close(config.CloseGrace)
			<-b.gone
		}()
	}
	wg.Wait()
	m.mu.Lock()
	d := m.display
	m.display = nil
	m.mu.Unlock()
	if d != nil {
		d.Close()
	}
}

// RunMaintenance closes idle browsers and forgets old tombstones until stop is closed.
func (m *Manager) RunMaintenance(stop <-chan struct{}) {
	tick := time.NewTicker(15 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tick.C:
		}
		m.CloseIdle(time.Now())
	}
}

// CloseIdle closes the browsers idle at now, and forgets the tombstones of groups gone an hour.
func (m *Manager) CloseIdle(now time.Time) {
	m.mu.Lock()
	if m.lim.IdleClose <= 0 {
		m.mu.Unlock()
		return
	}
	for id, gg := range m.gone {
		if now.Sub(gg.at) > time.Hour {
			delete(m.gone, id)
		}
	}
	var idle []*Browser
	for _, b := range m.browsers {
		// The operator's Chrome is never closed for idling: it is theirs, and open for them.
		if !b.attached && now.Sub(b.LastActivity()) >= m.lim.IdleClose {
			idle = append(idle, b)
		}
	}
	m.mu.Unlock()
	for _, b := range idle {
		if m.deps.Busy != nil && m.deps.Busy(b) {
			b.touch()
			continue
		}
		m.log.Info("closing an idle browser", "browser", b.ID)
		go func() {
			b.setClosing("idle")
			b.proc.Close(config.CloseGrace)
		}()
	}
}

// ProfileInfo is one profile on disk.
type ProfileInfo struct {
	ID         string    `json:"id"`
	SizeBytes  int64     `json:"size_bytes"`
	LastUsedAt time.Time `json:"last_used_at"`
	Running    bool      `json:"running"`
}

// Profiles lists the persistent profiles on disk.
func (m *Manager) Profiles() []ProfileInfo {
	if m.Attached() {
		// The operator's Chrome has its own profiles, which are none of the daemon's business.
		return nil
	}
	root := filepath.Join(m.deps.Config.StateDir, "profiles")
	entries, _ := os.ReadDir(root)
	var out []ProfileInfo
	for _, e := range entries {
		if !e.IsDir() || !ValidID(e.Name()) {
			continue
		}
		info := ProfileInfo{ID: e.Name()}
		_ = filepath.WalkDir(filepath.Join(root, e.Name()), func(_ string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			if fi, err := d.Info(); err == nil {
				info.SizeBytes += fi.Size()
				if fi.ModTime().After(info.LastUsedAt) {
					info.LastUsedAt = fi.ModTime().UTC()
				}
			}
			return nil
		})
		m.mu.Lock()
		info.Running = m.byProfile[e.Name()] != nil
		m.mu.Unlock()
		out = append(out, info)
	}
	return out
}

// ClearProfile removes a profile's cookies, storage and cache, keeping the directory; DeleteProfile
// removes it. Neither touches a profile whose browser runs.
func (m *Manager) ClearProfile(profile string, remove bool) error {
	if m.Attached() {
		return ErrAttachRefused("clearing or deleting a profile")
	}
	if !ValidID(profile) || profile == EphemeralProfile {
		return wire.Errorf(wire.CodeInvalidParams, "no such profile %q", profile)
	}
	m.mu.Lock()
	running := m.byProfile[profile] != nil || m.starting[profile] != nil
	m.mu.Unlock()
	if running {
		return wire.Errorf(wire.CodeForbidden, "profile %q is in use; close its browser first", profile)
	}
	dir := m.ProfileDir(profile)
	if _, err := os.Stat(dir); err != nil {
		return wire.Errorf(wire.CodeNotFound, "no profile %q", profile)
	}
	if remove {
		return os.RemoveAll(dir)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if err := os.RemoveAll(filepath.Join(dir, e.Name())); err != nil {
			return err
		}
	}
	return nil
}

// onEvent routes one event of a browser: its own session's to the target bookkeeping, a tab's to the
// tab and the listeners.
func (m *Manager) onEvent(b *Browser, e cdp.Event) {
	if e.Session == "" {
		m.browserEvent(b, e)
		for _, l := range m.listeners {
			l.BrowserEvent(b, e)
		}
		return
	}
	if m.workerEvent(e) {
		return
	}
	m.mu.Lock()
	t := m.bySession[e.Session]
	tab := t != nil
	frame := false
	if t == nil {
		// A page still being set up: its first navigation may pause already, and its first frames
		// attach, before it is a tab.
		t = m.settingUp[e.Session]
	}
	if t == nil {
		if t = m.frameTabs[e.Session]; t != nil {
			frame = true
		}
	}
	m.mu.Unlock()
	switch e.Method {
	case "Fetch.requestPaused":
		if t != nil && !frame {
			if atResponse(e.Params) {
				go m.responsePaused(t, e.Params)
			} else {
				go m.guardPaused(t, e.Params)
			}
		}
		return
	case "Target.attachedToTarget":
		go m.attachFrame(b, t, e.Session, e.Params)
		return
	case "Target.detachedFromTarget":
		if t != nil {
			m.detachFrame(t, e.Params)
		}
		return
	}
	// A frame's own events are not its tab's.
	if !tab {
		return
	}
	if b.attached && (e.Method == "Page.downloadWillBegin" || e.Method == "Page.downloadProgress") {
		// The operator's Chrome announces a download on the page that started it: the daemon sets no
		// download behaviour on a browser that is not its own, so there is no Browser event, and the
		// page model follows downloads by the Browser event's name and shape, which these share.
		be := cdp.Event{Method: "Browser." + strings.TrimPrefix(e.Method, "Page."), Params: e.Params}
		for _, l := range m.listeners {
			l.BrowserEvent(b, be)
		}
	}
	t.event(e)
	for _, l := range m.listeners {
		l.TabEvent(t, e)
	}
}

// attachFrame takes a target a page (or a frame of it) attached to. A frame of another site, which
// Chromium runs in a process of its own, is followed so the page model can read it: it gets the
// page's user agent and follows its own frames the same way, then runs. Anything else (a worker)
// runs as it is and is let go. Every one of them waits for the daemon (waitForDebuggerOnStart), so
// every one is let run, the tab gone or not.
func (m *Manager) attachFrame(b *Browser, t *Tab, parent string, raw json.RawMessage) {
	var p struct {
		SessionID  string     `json:"sessionId"`
		TargetInfo targetInfo `json:"targetInfo"`
	}
	if json.Unmarshal(raw, &p) != nil || p.SessionID == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if t == nil || t.Closed() || p.TargetInfo.Type != "iframe" {
		_ = b.conn.Call(ctx, p.SessionID, "Runtime.runIfWaitingForDebugger", nil, nil)
		_ = b.conn.Call(ctx, parent, "Target.detachFromTarget", map[string]any{"sessionId": p.SessionID}, nil)
		return
	}
	m.mu.Lock()
	m.frameTabs[p.SessionID] = t
	m.mu.Unlock()
	t.setFrame(p.TargetInfo.TargetID, p.SessionID)
	type call struct {
		method string
		params any
	}
	calls := []call{
		{"Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": true, "flatten": true}},
	}
	if !b.attached {
		calls = append(calls, call{"Emulation.setUserAgentOverride", map[string]any{"userAgent": b.userAgent, "userAgentMetadata": b.uaMetadata}})
	}
	// A frame of another site is a document of its own, and the page's script does not reach it. The
	// frame's session takes the script only with its Page domain on: without Page.enable the call
	// succeeds and the frame's document never runs it (measured on 155).
	script := b.screenScript(ctx)
	if script != "" {
		calls = append(calls, call{"Page.enable", nil},
			call{"Page.addScriptToEvaluateOnNewDocument", map[string]any{"source": script}})
	}
	var replies []<-chan error
	for _, call := range calls {
		replies = append(replies, b.conn.Send(ctx, p.SessionID, call.method, call.params))
	}
	// The agent's page scripts that run in every frame, before the frame runs, as the page's get them.
	var pending []pendingScript
	if b.hasFrameScripts() {
		if script == "" {
			replies = append(replies, b.conn.Send(ctx, p.SessionID, "Page.enable", nil))
		}
		pending = m.sendScripts(ctx, b, p.SessionID, true)
	}
	replies = append(replies, b.conn.Send(ctx, p.SessionID, "Runtime.runIfWaitingForDebugger", nil))
	for _, r := range replies {
		if err := <-r; err != nil {
			m.log.Debug("frame setup", "tab", t.ID, "error", err.Error())
		}
	}
	m.recordScripts(t, p.SessionID, pending)
}

// detachFrame forgets a frame's session when Chromium lets it go (the frame navigated to another
// process, or went).
func (m *Manager) detachFrame(t *Tab, raw json.RawMessage) {
	var p struct {
		SessionID string `json:"sessionId"`
	}
	if json.Unmarshal(raw, &p) != nil || p.SessionID == "" {
		return
	}
	m.mu.Lock()
	delete(m.frameTabs, p.SessionID)
	m.mu.Unlock()
	t.dropFrame(p.SessionID)
	t.dropScriptSession(p.SessionID)
}

type targetInfo struct {
	TargetID         string `json:"targetId"`
	Type             string `json:"type"`
	URL              string `json:"url"`
	Title            string `json:"title"`
	OpenerID         string `json:"openerId"`
	BrowserContextID string `json:"browserContextId"`
	// given is the operator's tab handed to the agent (relay.Tab.Given), which only the operator's
	// Chrome has; Chromium's own target info never says so.
	given bool
}

func (m *Manager) browserEvent(b *Browser, e cdp.Event) {
	switch e.Method {
	case "Target.attachedToTarget":
		var p struct {
			SessionID  string     `json:"sessionId"`
			TargetInfo targetInfo `json:"targetInfo"`
			Waiting    bool       `json:"waitingForDebuggerOnStart"`
		}
		if json.Unmarshal(e.Params, &p) != nil {
			return
		}
		if p.TargetInfo.Type != "page" {
			if m.workerAttached(b, e.Params) {
				return
			}
			// Workers and the like run as they are; the daemon follows pages only, and the workers of
			// the agent's own extensions.
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				_ = b.conn.Call(ctx, p.SessionID, "Runtime.runIfWaitingForDebugger", nil, nil)
				_ = b.conn.Call(ctx, "", "Target.detachFromTarget", map[string]any{"sessionId": p.SessionID}, nil)
			}()
			return
		}
		go m.attachPage(b, p.SessionID, p.TargetInfo)
	case "Target.targetInfoChanged":
		var p struct {
			TargetInfo targetInfo `json:"targetInfo"`
		}
		if json.Unmarshal(e.Params, &p) != nil {
			return
		}
		m.mu.Lock()
		t := m.byTarget[p.TargetInfo.TargetID]
		m.mu.Unlock()
		if t != nil {
			t.infoChanged(p.TargetInfo.URL, p.TargetInfo.Title)
		}
	case "Target.targetDestroyed", "Target.detachedFromTarget":
		var p struct {
			TargetID  string `json:"targetId"`
			SessionID string `json:"sessionId"`
		}
		if json.Unmarshal(e.Params, &p) != nil {
			return
		}
		m.forgetWorker(p.SessionID, p.TargetID)
		m.mu.Lock()
		t := m.byTarget[p.TargetID]
		if t == nil && p.SessionID != "" {
			t = m.bySession[p.SessionID]
		}
		if t != nil {
			m.forgetTabLocked(t)
		}
		m.mu.Unlock()
		if t != nil {
			m.tabGone(t)
			m.publish("tab.closed", map[string]any{"group_id": t.Group.ID, "tab_id": t.ID})
		}
	}
}
