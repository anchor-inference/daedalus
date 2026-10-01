// Package hooks is the per-launch side of the daemon: the registry of launches (each with its token,
// its overlay files, its dial directory and the ports it may be dialled on), and the loopback HTTP
// listener that turns a CLI's hook posts into events, holding a post open until the host replies
// when the CLI asks for an answer.
//
// A launch is how a program started in a terminal speaks back without typing on a screen: it is
// given a URL and a token in its environment, and whatever it posts there becomes an event tagged
// with its terminal and its launch. A post for a launch that has ended is refused, so a CLI that
// outlives its launch — or a copy of its settings run by hand — cannot put words in anyone's mouth.
package hooks

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// Errors of the registry.
var (
	ErrNoLaunch     = errors.New("no such launch")
	ErrLaunchExists = errors.New("a launch with this id exists")
	ErrTooMany      = errors.New("too many launches")
	ErrNoReply      = errors.New("no request is waiting for this reply")
	ErrStreams      = errors.New("too many streams open for this launch")
	ErrInvalid      = errors.New("invalid launch")
)

// validID is what a launch id may look like: the same shape as a terminal id, because it is a
// directory name and a URL segment.
var validID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// validFile is what each part of an overlay file's name may be. A name is one such part, or a few
// joined by '/' for a file a CLI only finds at a fixed place under a directory it is given (a skill
// at `.claude/skills/<name>/SKILL.md`); never '.', '..' or an absolute path, so a file can never be
// written outside the launch directory.
var validFile = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// maxFileDepth is how many parts an overlay file's name may have.
const maxFileDepth = 6

// checkFileName refuses a name that is not a few plain parts inside the launch directory; with
// nested false, exactly one.
func checkFileName(name string, nested bool) error {
	parts := strings.Split(name, "/")
	if len(parts) > maxFileDepth || (!nested && len(parts) > 1) {
		return fmt.Errorf("%w: file name %q: too deep a path", ErrInvalid, name)
	}
	for _, part := range parts {
		if !validFile.MatchString(part) || part == "." || part == ".." {
			return fmt.Errorf("%w: file name %q: parts of letters, digits, '.', '-' or '_' joined by '/'", ErrInvalid, name)
		}
	}
	return nil
}

// Publisher is where the registry's events go: the daemon's event log, so hook posts share the one
// sequence with terminal output and exits.
type Publisher interface {
	PublishSized(typ, terminalID string, data any, size int) events.Event
}

// Registry is every launch of the daemon.
type Registry struct {
	stateDir string
	ptyd     string // the daemon's own executable, for the commands a launch is given
	pub      Publisher
	log      *slog.Logger

	// Grace is how long a launch outlives its last terminal. Tests shorten it.
	Grace time.Duration

	realDials string // the dial root, symlinks resolved

	mu       sync.Mutex
	base     string // "http://127.0.0.1:<port>", once the listener is up
	launches map[string]*Launch
	replies  map[string]*held
	nheld    int
}

// Launch is one registered launch.
type Launch struct {
	ID      string
	Token   string
	Dir     string // the overlay files
	DialDir string // where the launch's programs put the unix sockets net.dial reaches
	Created time.Time

	reg     *Registry
	holdMax time.Duration
	limiter *bucket

	// Guarded by reg.mu.
	primary   string          // the terminal its events are tagged with
	terminals map[string]bool // every terminal started with it, and whether it still runs
	bound     bool
	ports     map[int]bool
	held      map[string]*held
	streams   map[int]func()
	nstream   int
	nput      int // files written by PutFile
	closed    bool
	gen       int // bumped by every change that invalidates a pending expiry
}

// NewRegistry returns an empty registry keeping its files under stateDir, and clears whatever a
// previous life of the daemon left there: its launches ended with its terminals.
func NewRegistry(stateDir, ptydPath string, pub Publisher, log *slog.Logger) (*Registry, error) {
	r := &Registry{stateDir: stateDir, ptyd: ptydPath, pub: pub, log: log, Grace: config.LaunchGrace,
		launches: map[string]*Launch{}, replies: map[string]*held{}}
	for _, dir := range []string{r.launchesDir(), r.dialsDir(), r.binDir()} {
		empty := os.RemoveAll
		if dir == r.binDir() {
			empty = clearBinDir // on Windows the hook command may be this very daemon's executable
		}
		if err := empty(dir); err != nil {
			return nil, err
		}
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
		if err := os.Chmod(dir, 0o700); err != nil {
			return nil, err
		}
	}
	// The hook command is one path, not "ptyd hook-post": a CLI's configuration may quote it as a
	// single word, and the daemon's path may hold a space. The link is the daemon itself, which
	// answers to the name it is called by.
	if err := linkHookCommand(ptydPath, r.HookCommand()); err != nil {
		return nil, err
	}
	real, err := filepath.EvalSymlinks(r.dialsDir())
	if err != nil {
		return nil, err
	}
	r.realDials = real
	return r, nil
}

// RealDialPath is where a socket of the launch is when nothing along the way is a link: the dial
// root as resolved at start, the launch's directory, the name.
func (r *Registry) RealDialPath(l *Launch, name string) string {
	return filepath.Join(r.realDials, l.ID, name)
}

// HookCommand is the command a launch's programs run to post a hook: `<command> <name>`.
func (r *Registry) HookCommand() string { return filepath.Join(r.binDir(), hookCommandName) }

func (r *Registry) binDir() string      { return filepath.Join(r.stateDir, "bin") }
func (r *Registry) launchesDir() string { return filepath.Join(r.stateDir, "launches") }
func (r *Registry) dialsDir() string    { return filepath.Join(r.stateDir, "dial") }

// SetBase is the listener's address, as the URL every launch is given.
func (r *Registry) SetBase(url string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.base = url
}

// Spec is a launch to register.
type Spec struct {
	ID         string            // empty: the daemon picks one
	TerminalID string            // the terminal its events are tagged with, when known in advance
	Files      map[string][]byte // written into the launch directory, 0600
	Ports      []int             // loopback ports net.dial may reach for it
	HoldMax    time.Duration     // the longest a post of it may be held; 0: the default
	TTL        time.Duration     // how long it waits for its first terminal; 0: the default
}

// Registered is what the host is told of a new launch.
type Registered struct {
	LaunchID string            `json:"launch_id"`
	HookURL  string            `json:"hook_url"`
	Token    string            `json:"hook_token"`
	Dir      string            `json:"dir"`
	DialDir  string            `json:"dial_dir"`
	Env      map[string]string `json:"env"`
	Files    []string          `json:"files"`
}

// Register creates a launch: its directory with the overlay files, its dial directory, its token.
func (r *Registry) Register(s Spec) (Registered, error) {
	if s.ID == "" {
		s.ID = "l" + randomHex(8)
	}
	if !validID.MatchString(s.ID) {
		return Registered{}, fmt.Errorf("%w: launch id must be 1-64 letters, digits, '-' or '_'", ErrInvalid)
	}
	if s.TerminalID != "" && !validID.MatchString(s.TerminalID) {
		return Registered{}, fmt.Errorf("%w: terminal id must be 1-64 letters, digits, '-' or '_'", ErrInvalid)
	}
	if len(s.Files) > config.MaxLaunchFiles {
		return Registered{}, fmt.Errorf("%w: at most %d files", ErrInvalid, config.MaxLaunchFiles)
	}
	names := make([]string, 0, len(s.Files))
	for name, data := range s.Files {
		if err := checkFileName(name, true); err != nil {
			return Registered{}, err
		}
		if len(data) > config.MaxLaunchFileBytes {
			return Registered{}, fmt.Errorf("%w: file %s is larger than %d bytes", ErrInvalid, name, config.MaxLaunchFileBytes)
		}
		names = append(names, name)
	}
	sort.Strings(names)
	ports := map[int]bool{}
	for _, p := range s.Ports {
		if p < 1 || p > 65535 {
			return Registered{}, fmt.Errorf("%w: port %d", ErrInvalid, p)
		}
		ports[p] = true
	}
	holdMax := s.HoldMax
	if holdMax <= 0 || holdMax > config.MaxHookHold {
		holdMax = config.DefaultHookHoldMax
	}
	ttl := s.TTL
	if ttl <= 0 {
		ttl = config.DefaultLaunchTTL
	}
	ttl = min(ttl, config.MaxLaunchTTL)

	r.mu.Lock()
	if _, ok := r.launches[s.ID]; ok {
		r.mu.Unlock()
		return Registered{}, ErrLaunchExists
	}
	if len(r.launches) >= config.MaxLaunches {
		r.mu.Unlock()
		return Registered{}, ErrTooMany
	}
	l := &Launch{ID: s.ID, Token: randomHex(32), Dir: filepath.Join(r.launchesDir(), s.ID),
		DialDir: filepath.Join(r.dialsDir(), s.ID), Created: time.Now().UTC(), reg: r, holdMax: holdMax,
		limiter: newBucket(config.HookRate, config.HookBurst), primary: s.TerminalID,
		terminals: map[string]bool{}, ports: ports, held: map[string]*held{}, streams: map[int]func(){}}
	// Reserved before the files are written, so a second register of the same id waits for nothing
	// and cannot write into this one's directory.
	l.closed = true
	r.launches[s.ID] = l
	base := r.base
	r.mu.Unlock()

	if err := l.writeFiles(s.Files); err != nil {
		r.mu.Lock()
		delete(r.launches, s.ID)
		r.mu.Unlock()
		_ = os.RemoveAll(l.Dir)
		_ = os.RemoveAll(l.DialDir)
		return Registered{}, err
	}
	r.mu.Lock()
	l.closed = false
	gen := l.gen
	r.mu.Unlock()
	time.AfterFunc(ttl, func() { r.expireUnbound(l, gen) })
	r.log.Info("launch registered", "launch", l.ID, "terminal", s.TerminalID, "files", len(names), "ports", len(ports))
	return Registered{LaunchID: l.ID, HookURL: base + "/hook/" + l.ID, Token: l.Token, Dir: l.Dir, DialDir: l.DialDir,
		Env: r.env(l, base), Files: names}, nil
}

// PutFile writes one more file into an open launch's directory — a message too long to type, which
// the CLI is told to read by its path — and returns where. The name is one plain part and the file
// must be new: nothing already in the directory, which the launch's own programs can write to, is
// ever followed or overwritten.
func (r *Registry) PutFile(launchID, name string, data []byte) (string, error) {
	if err := checkFileName(name, false); err != nil {
		return "", err
	}
	if len(data) > config.MaxLaunchFileBytes {
		return "", fmt.Errorf("%w: file %s is larger than %d bytes", ErrInvalid, name, config.MaxLaunchFileBytes)
	}
	r.mu.Lock()
	l := r.launches[launchID]
	if l == nil || l.closed {
		r.mu.Unlock()
		return "", ErrNoLaunch
	}
	if l.nput >= config.MaxLaunchPutFiles {
		r.mu.Unlock()
		return "", fmt.Errorf("%w: at most %d files may be added to a launch", ErrTooMany, config.MaxLaunchPutFiles)
	}
	l.nput++
	dir := l.Dir
	r.mu.Unlock()
	path := filepath.Join(dir, name)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|noFollow, 0o600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return "", fmt.Errorf("%w: file %s exists", ErrInvalid, name)
		}
		return "", err
	}
	_, werr := f.Write(data)
	if cerr := f.Close(); werr == nil {
		werr = cerr
	}
	if werr != nil {
		_ = os.Remove(path)
		return "", werr
	}
	return path, nil
}

func (l *Launch) writeFiles(files map[string][]byte) error {
	for _, dir := range []string{l.Dir, l.DialDir} {
		// Mkdir, not MkdirAll: the directory must be new, so nothing planted in advance is reused.
		if err := os.Mkdir(dir, 0o700); err != nil {
			return err
		}
	}
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	// The directories a nested name needs are made here, inside a directory that did not exist a
	// moment ago, so none of them can be a link planted in advance.
	made := map[string]bool{}
	for _, name := range names {
		data := files[name]
		parts := strings.Split(name, "/")
		for i := 1; i < len(parts); i++ {
			sub := filepath.Join(l.Dir, filepath.FromSlash(strings.Join(parts[:i], "/")))
			if made[sub] {
				continue
			}
			if err := os.Mkdir(sub, 0o700); err != nil {
				return err
			}
			made[sub] = true
		}
		f, err := os.OpenFile(filepath.Join(l.Dir, filepath.FromSlash(name)), os.O_WRONLY|os.O_CREATE|os.O_EXCL|noFollow, 0o600)
		if err != nil {
			return err
		}
		_, werr := f.Write(data)
		if cerr := f.Close(); werr == nil {
			werr = cerr
		}
		if werr != nil {
			return werr
		}
	}
	return nil
}

// env is what every terminal of the launch is given, and what the host may pass on to a program it
// configures by hand (an MCP server entry, say).
func (r *Registry) env(l *Launch, base string) map[string]string {
	return map[string]string{
		"DAEDALUS_LAUNCH_ID":  l.ID,
		"DAEDALUS_HOOK_URL":   base + "/hook/" + l.ID,
		"DAEDALUS_HOOK_TOKEN": l.Token,
		"DAEDALUS_HOOK_CMD":   r.HookCommand(),
		"DAEDALUS_PTYD_BIN":   r.ptyd,
		"DAEDALUS_DIAL_DIR":   l.DialDir,
		"DAEDALUS_LAUNCH_DIR": l.Dir,
	}
}

// Paths is what a program started with a launch reaches on disk: the overlay files, the dial
// directory, the directory of the hook command, and the daemon executable that command is. A
// sandbox that hides the daemon's state directory binds these back.
type Paths struct {
	Dir, Dial, Bin, Exe string
}

// Paths returns the launch's paths, and whether the launch is open.
func (r *Registry) Paths(launchID string) (Paths, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[launchID]
	if l == nil || l.closed {
		return Paths{}, false
	}
	return Paths{Dir: l.Dir, Dial: l.DialDir, Bin: r.binDir(), Exe: r.ptyd}, true
}

// Env is the environment of a terminal started with the launch, and whether the launch is open.
func (r *Registry) Env(launchID string) (map[string]string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[launchID]
	if l == nil || l.closed {
		return nil, false
	}
	return r.env(l, r.base), true
}

// Bind records a terminal that started with the launch: the launch now lives as long as any of its
// terminals runs, and its events are tagged with the first one unless the registration named
// another. It reports false when the launch has ended.
func (r *Registry) Bind(launchID, terminalID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[launchID]
	if l == nil || l.closed {
		return false
	}
	l.terminals[terminalID] = true
	l.bound = true
	l.gen++
	if l.primary == "" {
		l.primary = terminalID
	}
	return true
}

// TerminalExited records that a terminal of the launch ended. When it was the last one running, the
// launch is unregistered after the grace.
func (r *Registry) TerminalExited(launchID, terminalID string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[launchID]
	if l == nil || l.closed {
		return
	}
	if _, ok := l.terminals[terminalID]; !ok {
		return
	}
	l.terminals[terminalID] = false
	for _, running := range l.terminals {
		if running {
			return
		}
	}
	l.gen++
	gen := l.gen
	time.AfterFunc(r.Grace, func() {
		r.mu.Lock()
		stale := l.gen != gen || l.closed
		r.mu.Unlock()
		if !stale {
			r.Unregister(l.ID, "terminal_exited")
		}
	})
}

func (r *Registry) expireUnbound(l *Launch, gen int) {
	r.mu.Lock()
	stale := l.bound || l.closed || l.gen != gen
	r.mu.Unlock()
	if !stale {
		r.Unregister(l.ID, "expired")
	}
}

// Unregister ends a launch: its waiting posts are answered 410, its streams closed, its files and
// dial directory removed. It reports whether there was such a launch.
func (r *Registry) Unregister(id, reason string) bool {
	r.mu.Lock()
	l := r.launches[id]
	if l == nil || l.closed {
		r.mu.Unlock()
		return false
	}
	delete(r.launches, id)
	l.closed = true
	l.gen++
	waiting := make([]*held, 0, len(l.held))
	for _, h := range l.held {
		waiting = append(waiting, h)
		delete(r.replies, h.id)
		r.nheld--
	}
	l.held = map[string]*held{}
	closers := make([]func(), 0, len(l.streams))
	for _, c := range l.streams {
		closers = append(closers, c)
	}
	l.streams = map[int]func(){}
	terminal := l.primary
	r.mu.Unlock()

	for _, h := range waiting {
		h.deliver(Reply{Gone: true})
	}
	for _, c := range closers {
		c()
	}
	_ = os.RemoveAll(l.Dir)
	_ = os.RemoveAll(l.DialDir)
	r.pub.PublishSized("launch.ended", terminal, map[string]any{"launch_id": id, "reason": reason}, 0)
	r.log.Info("launch ended", "launch", id, "reason", reason)
	return true
}

// Close ends every launch; for the daemon's shutdown.
func (r *Registry) Close() {
	r.mu.Lock()
	ids := make([]string, 0, len(r.launches))
	for id := range r.launches {
		ids = append(ids, id)
	}
	r.mu.Unlock()
	for _, id := range ids {
		r.Unregister(id, "shutdown")
	}
}

// Get returns an open launch.
func (r *Registry) Get(id string) (*Launch, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[id]
	if l == nil || l.closed {
		return nil, false
	}
	return l, true
}

// Count is the number of open launches.
func (r *Registry) Count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.launches)
}

// Allow adds a loopback port net.dial may reach for the launch.
func (r *Registry) Allow(id string, port int) error {
	if port < 1 || port > 65535 {
		return fmt.Errorf("%w: port %d", ErrInvalid, port)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	l := r.launches[id]
	if l == nil || l.closed {
		return ErrNoLaunch
	}
	l.ports[port] = true
	return nil
}

// Allowed reports whether net.dial may reach port for the launch.
func (l *Launch) Allowed(port int) bool {
	l.reg.mu.Lock()
	defer l.reg.mu.Unlock()
	return !l.closed && l.ports[port]
}

// Primary is the terminal the launch's events are tagged with, "" before one is known.
func (l *Launch) Primary() string {
	l.reg.mu.Lock()
	defer l.reg.mu.Unlock()
	return l.primary
}

// AddStream records an open net.dial stream, closed by closeFn when the launch ends. The returned
// release forgets it once the stream has ended on its own.
func (l *Launch) AddStream(closeFn func()) (release func(), err error) {
	l.reg.mu.Lock()
	defer l.reg.mu.Unlock()
	if l.closed {
		return nil, ErrNoLaunch
	}
	if len(l.streams) >= config.MaxDialsPerLaunch {
		return nil, ErrStreams
	}
	l.nstream++
	n := l.nstream
	l.streams[n] = closeFn
	return func() {
		l.reg.mu.Lock()
		defer l.reg.mu.Unlock()
		delete(l.streams, n)
	}, nil
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
