//go:build linux

package memcap

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/ascorblack/daedalus/ptyd/internal/containment"
)

// hooks are the parts of the system a test replaces: a scratch directory stands in for the cgroup
// tree, and nothing is spawned or asked of systemd.
type hooks struct {
	pid       int
	delegated func(root string) bool
	probe     func(dir string) error
	systemctl func(args ...string) error
	machine   func() int64
	dropins   func(unit string) string // where set-property --runtime keeps the unit's drop-ins
}

// Manager is the cap of one daemon. Its methods are safe from any goroutine.
type Manager struct {
	mu        sync.Mutex
	h         hooks
	root      string // the group the daemon started in: its unit's, when systemd runs it
	unit      string // that unit's name, for systemctl; "" when the group is not a unit's
	mode      string // "jobs", "own" or "" (nothing can be capped)
	direct    bool   // in "own" mode, whether the group's files are written here rather than by systemd
	limited   string // the group whose files carry the cap: root/jobs, or root itself in "own" mode
	commands  *os.File
	reason    string
	note      string
	requested *Request
	stateFile string
}

// Setup finds the daemon's own group and prepares it to carry the cap, then applies the cap saved in
// stateFile by the last run, if any. It never fails: a Manager that cannot cap says why in Status.
func Setup(stateFile string) *Manager {
	h := hooks{pid: os.Getpid(), delegated: delegated, probe: probeClone, systemctl: systemctl, machine: machineBytes, dropins: runtimeDropins}
	root, err := containment.OwnRoot()
	if err != nil {
		return &Manager{h: h, stateFile: stateFile, reason: fmt.Sprintf("no cgroup v2 group of its own: %v", err)}
	}
	return setup(root, stateFile, h)
}

func setup(root, stateFile string, h hooks) *Manager {
	m := &Manager{h: h, root: root, unit: unitOf(root), stateFile: stateFile}
	if !alone(root, h.pid) {
		// A desktop daemon started without a scope of its own sits in the session's group with the
		// operator's other programs; a cap written there would hold those too.
		m.reason = "the daemon shares its control group with other programs, and a cap there would hold them too"
		return m
	}
	if h.delegated(root) {
		if err := m.split(); err != nil {
			m.reason = fmt.Sprintf("the delegated group could not be split: %v", err)
		} else {
			m.mode, m.reason = "jobs", ""
		}
	} else {
		m.reason = "the daemon's group is not delegated to it"
	}
	if m.mode == "" {
		switch {
		case writable(filepath.Join(root, "memory.max")):
			m.mode, m.direct, m.limited = "own", true, root
		case m.unit != "":
			m.mode, m.limited = "own", root
		default:
			m.reason += "; its memory limits are neither writable nor a systemd unit's"
			return m
		}
		// The daemon is in the capped group with its programs: the kernel still kills the largest
		// process first, which is a build and not the daemon, but nothing guarantees it.
		m.reason += ", so the cap holds the daemon as well as its programs"
	}
	if saved := loadRequest(stateFile); saved != nil {
		_, _ = m.Apply(*saved)
	} else if m.mode == "jobs" {
		// No cap yet: the unit's fallback limit holds, and jobs is kept under it so a kill at that
		// limit is a program's and the daemon keeps its room. A throttle point on the unit's group
		// goes at once, cap or not: it would hold the daemon (see lift).
		m.note = join(m.fitUnderRoot(), m.liftThrottle())
	}
	return m
}

// split moves the daemon into root/daemon and makes root/jobs, with root/jobs/commands for the
// programs. jobs is a group of its own, rather than the leaf the programs run in, so an attempt's
// containment can be made inside it and stay under the cap. Any failure puts everything back.
func (m *Manager) split() (err error) {
	root := m.root
	daemon, jobs := filepath.Join(root, "daemon"), filepath.Join(root, "jobs")
	commands := filepath.Join(jobs, "commands")
	enable := enabled(root)
	if !strings.Contains(enable, "+memory") {
		return errors.New("the memory controller is not available to the group")
	}
	if err := mkdir(daemon); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			m.unsplit(enable)
		}
	}()
	// A group that hands controllers to its children may hold no process itself, so the daemon goes
	// first. Writing the process id moves every thread with it.
	if err := write(filepath.Join(daemon, "cgroup.procs"), strconv.Itoa(m.h.pid)); err != nil {
		return fmt.Errorf("moving the daemon into its leaf: %w", err)
	}
	if err := write(filepath.Join(root, "cgroup.subtree_control"), enable); err != nil {
		return fmt.Errorf("enabling controllers: %w", err)
	}
	if err := mkdir(jobs); err != nil {
		return err
	}
	if err := write(filepath.Join(jobs, "cgroup.subtree_control"), enable); err != nil {
		return fmt.Errorf("enabling controllers for the programs: %w", err)
	}
	if err := mkdir(commands); err != nil {
		return err
	}
	// One process killed at the cap, not the whole group: a runaway test binary must not take the
	// operator's open shells with it.
	if err := write(filepath.Join(jobs, "memory.oom.group"), "0"); err != nil {
		return fmt.Errorf("memory.oom.group: %w", err)
	}
	if err := m.h.probe(commands); err != nil {
		return fmt.Errorf("starting a program straight into the group: %w", err)
	}
	file, err := os.Open(commands)
	if err != nil {
		return err
	}
	m.commands, m.limited = file, jobs
	return nil
}

func (m *Manager) unsplit(enable string) {
	disable := strings.ReplaceAll(enable, "+", "-")
	jobs := filepath.Join(m.root, "jobs")
	_ = write(filepath.Join(jobs, "cgroup.subtree_control"), disable)
	_ = os.Remove(filepath.Join(jobs, "commands"))
	_ = os.Remove(jobs)
	_ = write(filepath.Join(m.root, "cgroup.subtree_control"), disable)
	_ = write(filepath.Join(m.root, "cgroup.procs"), strconv.Itoa(m.h.pid))
	_ = os.Remove(filepath.Join(m.root, "daemon"))
}

// JobsDir is the capped group attempts are contained in, or "" when the daemon has none.
func (m *Manager) JobsDir() string {
	if m == nil || m.mode != "jobs" {
		return ""
	}
	return m.limited
}

// Apply sets the cap and returns what is in force after it. The request is saved for the next start
// whether or not the system took all of it, since the operator's choice has not changed.
func (m *Manager) Apply(r Request) (Status, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.mode == "" {
		return m.status(), errors.New(m.reason)
	}
	limits, note, err := Resolve(r, m.h.machine())
	if err != nil {
		return m.status(), err
	}
	m.requested, m.note = &r, note
	_ = saveRequest(m.stateFile, r)
	if m.mode == "jobs" || m.direct {
		for _, set := range [][2]string{
			{"memory.swap.max", value(limits.Swap)}, {"memory.high", value(limits.High)}, {"memory.max", value(limits.Max)},
		} {
			path := filepath.Join(m.limited, set[0])
			if _, err := os.Stat(path); err != nil && set[0] == "memory.swap.max" {
				// A kernel booted without swap accounting has no such file, and no swap to limit.
				continue
			}
			if err := write(path, set[1]); err != nil {
				return m.status(), fmt.Errorf("%s: %w", set[0], err)
			}
		}
		if m.mode == "jobs" {
			m.note = join(m.note, m.lift(limits))
			m.note = join(m.note, m.fitUnderRoot())
		}
		return m.status(), nil
	}
	// MemoryHigh is always infinity: this group holds the daemon (see lift).
	props := []string{"MemoryMax=" + unitValue(limits.Max), "MemoryHigh=" + unitValue(limits.High), "MemorySwapMax=" + unitValue(limits.Swap)}
	if err := m.h.systemctl(append([]string{"set-property", "--runtime", m.unit}, props...)...); err != nil {
		return m.status(), fmt.Errorf("systemd did not take the cap: %w", err)
	}
	m.dropScale(props)
	return m.status(), nil
}

// DaemonHeadroom is how far the unit's group must stay above the cap on jobs: the cap is reached,
// and its largest program killed, while the group the daemon shares with jobs still has room, so the
// daemon is never the one reclaimed from at the unit's limit.
const DaemonHeadroom = int64(512) << 20

// lift raises the unit's own limits, which systemd keeps on the group above both the daemon and
// jobs, when they would bind before the cap: they are the fallback the unit file carries for the time
// before the host first connects, and left in place they would quietly win over the operator's cap
// and act on the daemon too. A throttle point there is lifted whatever the cap: on 2026-11-18 the
// unit's MemoryHigh=50% throttled the whole group at once, the daemon with the builds, so a file stat
// took over ten seconds, the Files panel answered 503 and every host command hung.
//
// systemd owns that group's files, so it is asked rather than overwritten; --runtime lasts until a
// reboot, after which the fallback holds again until the cap is sent anew. A limit once set as a
// percentage lives in its own runtime drop-in (50-MemoryMaxScale.conf), which sorts after the absolute
// one set-property writes now and would put the percentage back at systemd's next reload, so the
// percentage drop-ins of what is lifted are removed.
func (m *Manager) lift(want Limits) string {
	var props []string
	for _, l := range []struct {
		file, prop string
		need       int64 // the least the unit's limit may be; 0 is none at all
	}{
		{"memory.max", "MemoryMax", headroom(want.Max)},
		{"memory.high", "MemoryHigh", 0},
		{"memory.swap.max", "MemorySwapMax", want.Swap},
	} {
		if l.prop == "MemorySwapMax" && want.Max != 0 {
			// The swap allowance is this daemon's policy, not the operator's choice: a unit that
			// allows less keeps it. Only "no limit" asks for the unit's to go.
			continue
		}
		have := readLimit(filepath.Join(m.root, l.file))
		if have >= 0 && have != none && (l.need == 0 || have < l.need) {
			props = append(props, l.prop+"=infinity")
		}
	}
	if len(props) == 0 {
		return ""
	}
	if m.unit == "" {
		return "the daemon's own group has a lower limit that only its owner can raise"
	}
	if err := m.h.systemctl(append([]string{"set-property", "--runtime", m.unit}, props...)...); err != nil {
		return fmt.Sprintf("the unit's own lower limit stays in force: %v", err)
	}
	m.dropScale(props)
	return ""
}

// dropScale removes the percentage drop-ins of the properties just set by set-property (see lift).
func (m *Manager) dropScale(props []string) {
	dir := m.h.dropins(m.unit)
	if dir == "" {
		return
	}
	for _, prop := range props {
		name, _, _ := strings.Cut(prop, "=")
		_ = os.Remove(filepath.Join(dir, "50-"+name+"Scale.conf"))
	}
}

// liftThrottle asks systemd to drop a throttle point on the unit's group, which holds the daemon.
func (m *Manager) liftThrottle() string {
	have := readLimit(filepath.Join(m.root, "memory.high"))
	if have < 0 || have == none {
		return ""
	}
	if m.unit == "" {
		return "the daemon's own group has a throttle point that only its owner can lift"
	}
	props := []string{"MemoryHigh=infinity"}
	if err := m.h.systemctl(append([]string{"set-property", "--runtime", m.unit}, props...)...); err != nil {
		return fmt.Sprintf("the unit's throttle point stays in force and slows the daemon too: %v", err)
	}
	m.dropScale(props)
	return ""
}

func headroom(max int64) int64 {
	if max == 0 {
		return 0
	}
	return max + DaemonHeadroom
}

// fitUnderRoot keeps the cap on jobs below the unit group's own limit by DaemonHeadroom, when that
// limit could not be lifted or the host has not sent a cap yet: the programs are then killed at jobs
// before the group that holds the daemon is full.
func (m *Manager) fitUnderRoot() string {
	outer := readLimit(filepath.Join(m.root, "memory.max"))
	if outer < 0 || outer == none {
		return ""
	}
	room := (outer - DaemonHeadroom) &^ 4095
	if room < MinBytes {
		room = MinBytes
	}
	jobs := readLimit(filepath.Join(m.limited, "memory.max"))
	if jobs >= 0 && jobs != none && jobs <= room {
		return ""
	}
	if err := write(filepath.Join(m.limited, "memory.max"), strconv.FormatInt(room, 10)); err != nil {
		return fmt.Sprintf("the cap could not be kept under the unit's own limit: %v", err)
	}
	return "the cap is kept under the unit's own limit, with room left for the daemon"
}

// Status reads back what is in force.
func (m *Manager) Status() Status {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.status()
}

func (m *Manager) status() Status {
	st := Status{Supported: m.mode != "", Mode: m.mode, Requested: m.requested, MachineBytes: m.h.machine(), Reason: join(m.reason, m.note)}
	if m.mode == "" {
		return st
	}
	limits := [3]int64{}
	for i, file := range []string{"memory.max", "memory.high", "memory.swap.max"} {
		limits[i] = readLimit(filepath.Join(m.limited, file))
		if m.mode == "jobs" {
			// The unit's group holds jobs too, so whichever of the two is lower is in force.
			limits[i] = lower(limits[i], readLimit(filepath.Join(m.root, file)))
		}
	}
	st.MaxBytes, st.HighBytes, st.SwapMaxBytes = public(limits[0]), public(limits[1]), public(limits[2])
	st.CurrentBytes = readNumber(filepath.Join(m.limited, "memory.current"))
	st.OOMKills = readKey(filepath.Join(m.limited, "memory.events"), "oom_kill")
	return st
}

// Kills is the oom_kill counter of the capped group: every program the kernel killed for the cap,
// including in nested groups. -1 when there is no cap.
func (m *Manager) Kills() int64 {
	if m == nil || m.mode == "" {
		return -1
	}
	return readKey(filepath.Join(m.limited, "memory.events"), "oom_kill")
}

// Place puts the program attrs start into the capped group as it is created (CLONE_INTO_CGROUP),
// so nothing it does before a later move could escape the cap. A program that already names a group
// of its own (an attempt's containment, itself inside jobs) keeps it.
func (m *Manager) Place(attrs *syscall.SysProcAttr) {
	if m == nil || m.commands == nil || attrs.UseCgroupFD {
		return
	}
	attrs.UseCgroupFD, attrs.CgroupFD = true, int(m.commands.Fd())
}

// Place is Manager.Place for the installed cap; nothing when there is none.
func Place(attrs *syscall.SysProcAttr) { Installed().Place(attrs) }

func alone(root string, pid int) bool {
	data, err := os.ReadFile(filepath.Join(root, "cgroup.procs"))
	if err != nil {
		return false
	}
	fields := strings.Fields(string(data))
	return len(fields) == 1 && fields[0] == strconv.Itoa(pid)
}

// delegated says whether systemd handed the group over (Delegate=yes). The system manager marks such
// a group with an extended attribute; a user manager, which is what runs the host terminal, does not
// (systemd 255 left none on a delegated user unit), so its unit is asked instead.
func delegated(root string) bool {
	buf := make([]byte, 8)
	for _, name := range []string{"user.delegate", "trusted.delegate"} {
		if n, err := unix.Getxattr(root, name, buf); err == nil && n > 0 && buf[0] == '1' {
			return true
		}
	}
	unit := unitOf(root)
	if unit == "" {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl", "--user", "show", "--property=Delegate", "--value", unit).Output()
	return err == nil && strings.TrimSpace(string(out)) == "yes"
}

// probeClone proves a program can be started straight into dir, as every spawn will be.
func probeClone(dir string) error {
	file, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer file.Close()
	program := "/bin/true"
	if _, err := os.Stat(program); err != nil {
		program = "/usr/bin/true"
	}
	cmd := exec.Command(program)
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: int(file.Fd())}
	return cmd.Run()
}

func systemctl(args ...string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "systemctl", append([]string{"--user"}, args...)...).CombinedOutput()
	if err != nil {
		if text := strings.TrimSpace(string(out)); text != "" {
			return fmt.Errorf("%w: %s", err, text)
		}
		return err
	}
	return nil
}

func runtimeDropins(unit string) string {
	dir := os.Getenv("XDG_RUNTIME_DIR")
	if dir == "" || unit == "" {
		return ""
	}
	return filepath.Join(dir, "systemd", "user.control", unit+".d")
}

func machineBytes() int64 {
	file, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0
	}
	defer file.Close()
	scan := bufio.NewScanner(file)
	for scan.Scan() {
		fields := strings.Fields(scan.Text())
		if len(fields) >= 2 && fields[0] == "MemTotal:" {
			kb, _ := strconv.ParseInt(fields[1], 10, 64)
			return kb << 10
		}
	}
	return 0
}

func unitOf(root string) string {
	name := filepath.Base(root)
	if strings.HasSuffix(name, ".service") || strings.HasSuffix(name, ".scope") {
		return name
	}
	return ""
}

// enabled is the "+cpu +memory +pids" this group may hand to its children: those of the three it has.
func enabled(root string) string {
	data, _ := os.ReadFile(filepath.Join(root, "cgroup.controllers"))
	have := map[string]bool{}
	for _, name := range strings.Fields(string(data)) {
		have[name] = true
	}
	var out []string
	for _, name := range []string{"cpu", "memory", "pids"} {
		if have[name] {
			out = append(out, "+"+name)
		}
	}
	return strings.Join(out, " ")
}

func writable(path string) bool { return unix.Access(path, unix.W_OK) == nil }

func mkdir(path string) error {
	if err := os.Mkdir(path, 0o755); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	return nil
}

func write(path, text string) error {
	return os.WriteFile(path, []byte(text), 0o644)
}

func value(n int64) string {
	if n <= 0 {
		return "max"
	}
	return strconv.FormatInt(n, 10)
}

func unitValue(n int64) string {
	if n <= 0 {
		return "infinity"
	}
	return strconv.FormatInt(n, 10)
}

// none is a limit file's "max": no limit.
const none = math.MaxInt64

// readLimit reads a limit file: none for "max", -1 when it cannot be read.
func readLimit(path string) int64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	text := string(bytes.TrimSpace(data))
	if text == "max" {
		return none
	}
	n, err := strconv.ParseInt(text, 10, 64)
	if err != nil {
		return -1
	}
	return n
}

func readNumber(path string) int64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	n, err := strconv.ParseInt(string(bytes.TrimSpace(data)), 10, 64)
	if err != nil {
		return -1
	}
	return n
}

func readKey(path, key string) int64 {
	data, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[0] == key {
			if n, err := strconv.ParseInt(fields[1], 10, 64); err == nil {
				return n
			}
		}
	}
	return -1
}

// lower is the stricter of two limits read by readLimit; an unknown one does not count.
func lower(a, b int64) int64 {
	switch {
	case a < 0:
		return b
	case b < 0:
		return a
	}
	return min(a, b)
}

// public is a limit as Status carries it: nil for none, and for one that could not be read.
func public(n int64) *int64 {
	if n < 0 || n == none {
		return nil
	}
	return &n
}

func join(a, b string) string {
	switch {
	case a == "":
		return b
	case b == "":
		return a
	}
	return a + "; " + b
}
