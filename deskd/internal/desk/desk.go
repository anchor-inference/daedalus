// Package desk is the daemon's rules over a backend, the same on every system: the operator's lease
// and its scope, screenshot ids that bind a pixel to the desktop it was read from, the pause when the
// operator touches the machine, the kill switch, the limits, the apps the daemon never touches, and
// the audit. It is the one authority that keeps working when the host hangs: whatever the host sends,
// no input reaches the desktop outside an active lease.
package desk

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// The defaults, one name each, as the contract's Limits table lists them.
const (
	DefaultActionsPerSecond = 4
	DefaultActionBurst      = 4
	DefaultLeaseActions     = 200
	DefaultLeaseTTL         = 30 * time.Minute
	DefaultMaxLeaseTTL      = 4 * time.Hour
	DefaultHumanQuiet       = 3 * time.Second
	DefaultMaxTypeChars     = 2000
	DefaultMaxWait          = 10 * time.Second
	DefaultScreenshotTTL    = 10 * time.Minute
	DefaultMaxEdge          = 1568
	DefaultJPEGQuality      = 80
	// keptScreenshots is how many screenshot ids stay valid at once: enough for a model that looks at
	// two displays and a zoomed window in one turn, few enough that an id from long ago is refused.
	keptScreenshots = 16
	// humanTolerance is how far, in desktop units, the pointer may sit from where the daemon left it
	// before the difference is the operator's hand: a system may round a synthetic move by a unit.
	humanTolerance = 2.0
	// humanGrace is how long after the daemon's own last input the system's last-input time is still
	// taken to be the daemon's: the event reaches the system a moment after the call returns.
	humanGrace = 300 * time.Millisecond
	// pollActive and pollIdle are how often the operator's input, the lease's expiry, the displays and
	// the permissions are looked at with a lease and without one.
	pollActive = 100 * time.Millisecond
	pollIdle   = time.Second
	// pollState is how often displays and permissions are read while a lease is active.
	pollState = 2 * time.Second
	// maxScreenshotData is the largest base64 picture a reply carries: a frame is 1 MiB, and the reply
	// has its geometry around the picture.
	maxScreenshotData = 1000 << 10
)

// Matcher names an app. Every field that is set must match; an empty matcher matches nothing.
type Matcher struct {
	BundleID string `json:"bundle_id,omitempty"`
	// ExePath is the executable's full path; ExeName its base name, compared without case and
	// without ".exe", so one entry covers a Windows and a Linux build.
	ExePath string `json:"exe_path,omitempty"`
	ExeName string `json:"exe_name,omitempty"`
	// WMClass matches either half of X11's WM_CLASS ("instance.Class"), without case.
	WMClass string `json:"wm_class,omitempty"`
	PID     int    `json:"pid,omitempty"`
}

// Match reports whether the app is the one named.
func (m Matcher) Match(a backend.App) bool {
	if m == (Matcher{}) {
		return false
	}
	if m.BundleID != "" && m.BundleID != a.BundleID {
		return false
	}
	if m.ExePath != "" && m.ExePath != a.ExePath {
		return false
	}
	if m.ExeName != "" {
		base := exeBase(a.ExePath)
		if base == "" || !strings.EqualFold(strings.TrimSuffix(strings.ToLower(m.ExeName), ".exe"), base) {
			return false
		}
	}
	if m.WMClass != "" {
		found := false
		for _, part := range strings.Split(a.WMClass, ".") {
			if part != "" && strings.EqualFold(part, m.WMClass) {
				found = true
			}
		}
		if !found && !strings.EqualFold(a.WMClass, m.WMClass) {
			return false
		}
	}
	if m.PID != 0 && m.PID != a.PID {
		return false
	}
	return true
}

// exeBase is an executable's base name in lower case without ".exe", for paths of any system.
func exeBase(path string) string {
	if path == "" {
		return ""
	}
	path = strings.ReplaceAll(path, `\`, "/")
	return strings.TrimSuffix(strings.ToLower(filepath.Base(path)), ".exe")
}

// Config is the daemon's limits and its protected apps.
type Config struct {
	ActionsPerSecond float64 `json:"actions_per_second,omitempty"`
	ActionBurst      int     `json:"action_burst,omitempty"`
	// LeaseActions is how many actions one lease allows; the operator's grant may lower it.
	LeaseActions int `json:"lease_actions,omitempty"`
	// DefaultLeaseMs and MaxLeaseMs bound a lease's life.
	DefaultLeaseMs int64 `json:"default_lease_ms,omitempty"`
	MaxLeaseMs     int64 `json:"max_lease_ms,omitempty"`
	// HumanQuietMs is how long the operator must leave the mouse and keyboard alone before the agent
	// may act again. Zero leaves the agent paused until the operator resumes it.
	HumanQuietMs   *int64 `json:"human_quiet_ms,omitempty"`
	MaxTypeChars   int    `json:"max_type_chars,omitempty"`
	MaxWaitMs      int64  `json:"max_wait_ms,omitempty"`
	ScreenshotTTLs int64  `json:"screenshot_ttl_s,omitempty"`
	MaxEdge        int    `json:"max_edge,omitempty"`
	// Protected are the apps no input may reach and no lease may scope to: Daedalus's own windows
	// first, so the agent can never press "allow" on its own approval card.
	Protected []Matcher `json:"protected,omitempty"`
}

// withDefaults fills every unset limit.
func (c Config) withDefaults() Config {
	if c.ActionsPerSecond <= 0 {
		c.ActionsPerSecond = DefaultActionsPerSecond
	}
	if c.ActionBurst <= 0 {
		c.ActionBurst = DefaultActionBurst
	}
	if c.LeaseActions <= 0 {
		c.LeaseActions = DefaultLeaseActions
	}
	if c.DefaultLeaseMs <= 0 {
		c.DefaultLeaseMs = DefaultLeaseTTL.Milliseconds()
	}
	if c.MaxLeaseMs <= 0 {
		c.MaxLeaseMs = DefaultMaxLeaseTTL.Milliseconds()
	}
	if c.HumanQuietMs == nil {
		q := DefaultHumanQuiet.Milliseconds()
		c.HumanQuietMs = &q
	}
	if c.MaxTypeChars <= 0 {
		c.MaxTypeChars = DefaultMaxTypeChars
	}
	if c.MaxWaitMs <= 0 {
		c.MaxWaitMs = DefaultMaxWait.Milliseconds()
	}
	if c.ScreenshotTTLs <= 0 {
		c.ScreenshotTTLs = int64(DefaultScreenshotTTL.Seconds())
	}
	if c.MaxEdge <= 0 {
		c.MaxEdge = DefaultMaxEdge
	}
	return c
}

// Limits is the part of the configuration the host is told in info.
type Limits struct {
	ActionsPerSecond float64 `json:"actions_per_second"`
	ActionBurst      int     `json:"action_burst"`
	LeaseActions     int     `json:"lease_actions"`
	DefaultLeaseMs   int64   `json:"default_lease_ms"`
	MaxLeaseMs       int64   `json:"max_lease_ms"`
	HumanQuietMs     int64   `json:"human_quiet_ms"`
	MaxTypeChars     int     `json:"max_type_chars"`
	MaxWaitMs        int64   `json:"max_wait_ms"`
	ScreenshotTTLs   int64   `json:"screenshot_ttl_s"`
	MaxEdge          int     `json:"max_edge"`
}

// Desk is the daemon's state.
type Desk struct {
	cfg    Config
	be     backend.Backend
	events *events.Log
	audit  *audit.Log
	log    *slog.Logger
	now    func() time.Time

	// input serialises everything that sends input, so two actions never interleave their events
	// and stop can wait for the one in flight to let go of its keys.
	input sync.Mutex

	mu         sync.Mutex
	lease      *Lease
	ended      endedLease // the last lease that ended, to say why a call has none
	actCancel  context.CancelFunc
	shots      map[string]*Shot
	shotOrder  []string
	shotSerial uint64
	shotPrefix string

	displays   []geom.Display
	displayGen int
	perms      backend.Permissions

	// The operator's hand.
	paused    bool
	pausedAt  time.Time
	lastHuman time.Time
	injecting bool
	// injectSerial changes at the start and the end of every step that sends input, so the watch can
	// tell that one happened while it was reading.
	injectSerial uint64
	lastInject   time.Time
	// lastSeenInput is the latest last-input time the watch has read, so one burst of the operator's
	// input is counted once and the quiet period can begin after it.
	lastSeenInput time.Time
	ourX, ourY    float64
	ourKnown      bool

	// The rate limit: a bucket of action tokens.
	tokens     float64
	refilledAt time.Time

	// What the daemon is holding down, released by stop and by any action that fails half way.
	heldButtons map[backend.Button]bool
	heldKeys    []heldKey
}

// New returns the rules over be.
func New(cfg Config, be backend.Backend, evlog *events.Log, auditLog *audit.Log, log *slog.Logger) *Desk {
	prefix := make([]byte, 4)
	_, _ = rand.Read(prefix)
	cfg = cfg.withDefaults()
	return &Desk{cfg: cfg, be: be, events: evlog, audit: auditLog, log: log, now: time.Now,
		shots: map[string]*Shot{}, shotPrefix: hex.EncodeToString(prefix), tokens: float64(cfg.ActionBurst),
		heldButtons: map[backend.Button]bool{}}
}

// Backend is the desktop the rules apply to.
func (d *Desk) Backend() backend.Backend { return d.be }

// Limits is the effective configuration.
func (d *Desk) Limits() Limits {
	c := d.cfg
	return Limits{ActionsPerSecond: c.ActionsPerSecond, ActionBurst: c.ActionBurst, LeaseActions: c.LeaseActions,
		DefaultLeaseMs: c.DefaultLeaseMs, MaxLeaseMs: c.MaxLeaseMs, HumanQuietMs: *c.HumanQuietMs,
		MaxTypeChars: c.MaxTypeChars, MaxWaitMs: c.MaxWaitMs, ScreenshotTTLs: c.ScreenshotTTLs, MaxEdge: c.MaxEdge}
}

// Protected reports whether no input may reach the app.
func (d *Desk) Protected(a backend.App) bool {
	for _, m := range d.cfg.Protected {
		if m.Match(a) {
			return true
		}
	}
	return false
}

func (d *Desk) write(r audit.Record) {
	if err := d.audit.Write(r); err != nil {
		d.log.Warn("audit write failed", "error", err.Error())
	}
}

// Info is the state info reports.
type Info struct {
	Platform    backend.Platform    `json:"platform"`
	Backend     string              `json:"backend"`
	Displays    []geom.Display      `json:"displays"`
	Permissions backend.Permissions `json:"permissions"`
	Secure      backend.Secure      `json:"secure"`
	Lease       LeaseStatus         `json:"lease"`
	Human       HumanState          `json:"human"`
	Limits      Limits              `json:"limits"`
}

// HumanState is whether the agent is paused for the operator's hand.
type HumanState struct {
	Paused bool `json:"paused"`
	// Since is when the pause began; LastInput when the operator's last input was seen.
	Since     *time.Time `json:"since,omitempty"`
	LastInput *time.Time `json:"last_input,omitempty"`
	// ResumesAt is when the quiet period ends if the operator stays away; absent when only the
	// operator's resume ends the pause.
	ResumesAt *time.Time `json:"resumes_at,omitempty"`
}

// Info reads the desktop's state now.
func (d *Desk) Info(ctx context.Context) (Info, error) {
	ds, err := d.refreshDisplays(ctx)
	if err != nil {
		return Info{}, fromBackend(err)
	}
	perms := d.be.Permissions(ctx)
	sec, err := d.be.Secure(ctx)
	if err != nil {
		d.log.Warn("secure state", "error", err.Error())
	}
	p := d.be.Platform()
	return Info{Platform: p, Backend: p.SessionType, Displays: ds, Permissions: perms, Secure: sec,
		Lease: d.LeaseStatus(), Human: d.HumanState(), Limits: d.Limits()}, nil
}

// HumanState reads the pause.
func (d *Desk) HumanState() HumanState {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.humanStateLocked()
}

func (d *Desk) humanStateLocked() HumanState {
	if !d.paused {
		return HumanState{}
	}
	since, last := d.pausedAt, d.lastHuman
	h := HumanState{Paused: true, Since: &since, LastInput: &last}
	if q := *d.cfg.HumanQuietMs; q > 0 {
		at := last.Add(time.Duration(q) * time.Millisecond)
		h.ResumesAt = &at
	}
	return h
}

// refreshDisplays reads the displays and, when they changed, invalidates every screenshot taken of
// the old arrangement and tells the subscribers.
func (d *Desk) refreshDisplays(ctx context.Context) ([]geom.Display, error) {
	ds, err := d.be.Displays(ctx)
	if err != nil {
		return nil, err
	}
	d.mu.Lock()
	changed := d.displays != nil && !sameDisplays(d.displays, ds)
	if changed || d.displays == nil {
		d.displays = ds
		if changed {
			d.displayGen++
		}
	}
	d.mu.Unlock()
	if changed {
		d.events.Publish("displays.changed", "", map[string]any{"displays": ds})
	}
	return ds, nil
}

func sameDisplays(a, b []geom.Display) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// Run watches the lease's expiry, the operator's hand, the displays and the permissions until stop
// is closed.
func (d *Desk) Run(stop <-chan struct{}) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	d.mu.Lock()
	d.perms = d.be.Permissions(ctx)
	d.mu.Unlock()
	var lastState time.Time
	for {
		active := d.Tick(ctx)
		if now := d.now(); !active || now.Sub(lastState) >= pollState {
			lastState = now
			d.pollState(ctx)
		}
		wait := pollIdle
		if active {
			wait = pollActive
		}
		select {
		case <-stop:
			return
		case <-time.After(wait):
		}
	}
}

// pollState reads the displays and the permissions and publishes a change.
func (d *Desk) pollState(ctx context.Context) {
	if _, err := d.refreshDisplays(ctx); err != nil {
		d.log.Debug("displays", "error", err.Error())
	}
	perms := d.be.Permissions(ctx)
	d.mu.Lock()
	changed := perms != d.perms
	d.perms = perms
	d.mu.Unlock()
	if changed {
		d.events.Publish("permissions.changed", "", map[string]any{"permissions": perms})
	}
}

// Tick does one round of the watch and reports whether a lease is active. Tests call it directly.
func (d *Desk) Tick(ctx context.Context) bool {
	d.expire()
	d.mu.Lock()
	active := d.lease != nil
	d.mu.Unlock()
	if active {
		d.watchHuman(ctx)
	}
	return active
}
