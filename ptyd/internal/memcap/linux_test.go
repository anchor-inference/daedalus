//go:build linux

package memcap

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
)

const testMachine = int64(64) << 30

// fakeGroup is a scratch directory shaped like the group systemd hands a unit: a regular directory
// takes the writes a kernel group would, so what the daemon writes where can be read back.
func fakeGroup(t *testing.T, files map[string]string) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "daedalus-ptyd.service")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	base := map[string]string{"cgroup.procs": "4242\n", "cgroup.controllers": "cpu io memory pids\n", "cgroup.subtree_control": "",
		"memory.max": "max\n", "memory.high": "max\n", "memory.swap.max": "max\n"}
	for name, text := range files {
		base[name] = text
	}
	for name, text := range base {
		if err := os.WriteFile(filepath.Join(root, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

// calls records what systemd was asked. With root set it answers as systemd does, writing "max" into
// the unit's group for each property lifted; with refuse set it refuses.
type calls struct {
	systemctl [][]string
	root      string
	refuse    bool
	dropins   string
}

var unitFiles = map[string]string{"MemoryMax": "memory.max", "MemoryHigh": "memory.high", "MemorySwapMax": "memory.swap.max"}

func testHooks(c *calls, delegated bool) hooks {
	return hooks{
		pid:       4242,
		delegated: func(string) bool { return delegated },
		// The kernel gives a new group its interface files; the scratch copy gets the one the writer
		// checks for before writing (a kernel without swap accounting has none).
		probe: func(dir string) error {
			return os.WriteFile(filepath.Join(filepath.Dir(dir), "memory.swap.max"), []byte("max\n"), 0o644)
		},
		systemctl: func(args ...string) error {
			c.systemctl = append(c.systemctl, args)
			if c.refuse {
				return errors.New("Failed to connect to bus")
			}
			for _, arg := range args {
				if prop, value, ok := strings.Cut(arg, "="); ok && c.root != "" && unitFiles[prop] != "" && value == "infinity" {
					_ = os.WriteFile(filepath.Join(c.root, unitFiles[prop]), []byte("max\n"), 0o644)
				}
			}
			return nil
		},
		machine: func() int64 { return testMachine },
		dropins: func(string) string { return c.dropins },
	}
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(data))
}

func TestResolve(t *testing.T) {
	limits, note, err := Resolve(Request{Percent: 60}, testMachine)
	if err != nil || note != "" {
		t.Fatal(err, note)
	}
	if want := testMachine * 6 / 10 &^ 4095; limits.Max != want {
		t.Fatalf("max %d, want %d", limits.Max, want)
	}
	if limits.High != 0 || limits.Swap != SwapBytes {
		t.Fatalf("no throttle point below the cap, and the small swap: %+v", limits)
	}
	if limits, _, _ := Resolve(Request{Unlimited: true}, testMachine); limits != (Limits{}) {
		t.Fatalf("no limit must be all zero: %+v", limits)
	}
	if limits, note, _ := Resolve(Request{Bytes: testMachine * 2}, testMachine); limits.Max != testMachine || note == "" {
		t.Fatalf("a cap past the machine is held to the machine and says so: %+v %q", limits, note)
	}
	for _, bad := range []Request{{}, {Percent: 50, Bytes: 1 << 30}, {Percent: 0.5}, {Percent: 101}, {Bytes: 1 << 20}, {Percent: 10, Unlimited: true}} {
		if _, _, err := Resolve(bad, testMachine); err == nil {
			t.Fatalf("%+v must be refused", bad)
		}
	}
}

func TestDelegatedGroupIsSplitAndTheCapGoesOnJobs(t *testing.T) {
	root := fakeGroup(t, map[string]string{"memory.max": "38654705664\n", "memory.high": "34359738368\n", "memory.swap.max": "2147483648\n"})
	var c calls
	m := setup(root, filepath.Join(t.TempDir(), "cap.json"), testHooks(&c, true))
	if m.mode != "jobs" {
		t.Fatalf("mode %q: %s", m.mode, m.reason)
	}
	if got := read(t, filepath.Join(root, "daemon", "cgroup.procs")); got != "4242" {
		t.Fatalf("the daemon was not moved into its own leaf: %q", got)
	}
	if got := read(t, filepath.Join(root, "cgroup.subtree_control")); got != "+cpu +memory +pids" {
		t.Fatalf("root hands %q to its children", got)
	}
	if got := read(t, filepath.Join(root, "jobs", "cgroup.subtree_control")); got != "+cpu +memory +pids" {
		t.Fatalf("jobs hands %q to its children", got)
	}
	if got := read(t, filepath.Join(root, "jobs", "memory.oom.group")); got != "0" {
		t.Fatalf("a kill at the cap must take one process, not the group: %q", got)
	}
	if m.JobsDir() != filepath.Join(root, "jobs") {
		t.Fatalf("attempts belong under the cap, not beside it: %q", m.JobsDir())
	}
	var attrs syscall.SysProcAttr
	m.Place(&attrs)
	if !attrs.UseCgroupFD || attrs.CgroupFD <= 0 {
		t.Fatal("a program must be started straight into the capped group")
	}
	contained := syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: 99}
	m.Place(&contained)
	if contained.CgroupFD != 99 {
		t.Fatal("an attempt's own group must not be replaced")
	}

	// Before the host sends a cap, the unit's fallback limit holds and jobs sits under it with room
	// left for the daemon, so a kill at that limit is a program's.
	jobs := filepath.Join(root, "jobs")
	if got := read(t, filepath.Join(jobs, "memory.max")); got != itoa(38654705664-DaemonHeadroom) {
		t.Fatalf("jobs before the first cap: %q", got)
	}

	c.root = root
	status, err := m.Apply(Request{Percent: 60})
	if err != nil {
		t.Fatal(err)
	}
	want, _, _ := Resolve(Request{Percent: 60}, testMachine)
	if read(t, filepath.Join(jobs, "memory.max")) != itoa(want.Max) || read(t, filepath.Join(jobs, "memory.high")) != "max" ||
		read(t, filepath.Join(jobs, "memory.swap.max")) != itoa(SwapBytes) {
		t.Fatal("the cap was not written to jobs")
	}
	// The unit's fallback (MemoryMax=60%) would bind with the cap at the same point, and any throttle
	// point there holds the daemon too: systemd is asked to lift both. The swap limit, already as
	// small as the cap's, is left alone.
	// (The first call is the start's: the throttle point goes before any cap is sent. The stand-in
	// systemd did not write then, so it is asked again.)
	if len(c.systemctl) != 2 || strings.Join(c.systemctl[0], " ") != "set-property --runtime daedalus-ptyd.service MemoryHigh=infinity" ||
		strings.Join(c.systemctl[1], " ") != "set-property --runtime daedalus-ptyd.service MemoryMax=infinity MemoryHigh=infinity" {
		t.Fatalf("systemctl calls %v", c.systemctl)
	}
	if Bytes(status.MaxBytes) != want.Max || status.HighBytes != nil || Bytes(status.SwapMaxBytes) != SwapBytes || !status.Supported || status.Mode != "jobs" {
		t.Fatalf("status must report what is in force: %+v", status)
	}
	if err := os.WriteFile(filepath.Join(jobs, "memory.events"), []byte("low 0\nhigh 3\nmax 1\noom 1\noom_kill 2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(jobs, "memory.current"), []byte("123456\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	status = m.Status()
	if Bytes(status.MaxBytes) != want.Max || Bytes(status.HighBytes) != want.High || status.CurrentBytes != 123456 || status.OOMKills != 2 || m.Kills() != 2 {
		t.Fatalf("status %+v", status)
	}

	if _, err := m.Apply(Request{Unlimited: true}); err != nil {
		t.Fatal(err)
	}
	if read(t, filepath.Join(jobs, "memory.max")) != "max" || read(t, filepath.Join(jobs, "memory.high")) != "max" || read(t, filepath.Join(jobs, "memory.swap.max")) != "max" {
		t.Fatal("no limit must clear every limit on jobs")
	}
}

func TestSavedCapIsAppliedAtTheNextStart(t *testing.T) {
	state := filepath.Join(t.TempDir(), "cap.json")
	var c calls
	first := setup(fakeGroup(t, nil), state, testHooks(&c, true))
	if _, err := first.Apply(Request{Bytes: 8 << 30}); err != nil {
		t.Fatal(err)
	}
	root := fakeGroup(t, nil)
	second := setup(root, state, testHooks(&c, true))
	if got := read(t, filepath.Join(root, "jobs", "memory.max")); got != itoa(8<<30) {
		t.Fatalf("a restarted daemon must hold its programs to the last cap before the host reconnects: %q", got)
	}
	if second.Status().Requested == nil || second.Status().Requested.Bytes != 8<<30 {
		t.Fatal("the saved request is reported")
	}
}

func TestUndelegatedGroupIsCappedWhole(t *testing.T) {
	root := fakeGroup(t, nil)
	var c calls
	m := setup(root, "", testHooks(&c, false))
	if m.mode != "own" || !m.direct || m.JobsDir() != "" {
		t.Fatalf("mode %q direct %v", m.mode, m.direct)
	}
	var attrs syscall.SysProcAttr
	m.Place(&attrs)
	if attrs.UseCgroupFD {
		t.Fatal("without a split, programs stay in the daemon's group")
	}
	status, err := m.Apply(Request{Bytes: 4 << 30})
	if err != nil {
		t.Fatal(err)
	}
	if read(t, filepath.Join(root, "memory.max")) != itoa(4<<30) || Bytes(status.MaxBytes) != 4<<30 || len(c.systemctl) != 0 {
		t.Fatalf("own group not written directly: %+v %v", status, c.systemctl)
	}
	if !strings.Contains(status.Reason, "holds the daemon") {
		t.Fatalf("the status must say the daemon is under its own cap: %q", status.Reason)
	}
}

func TestReadOnlyGroupGoesThroughSystemd(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root writes a read-only file")
	}
	root := fakeGroup(t, nil)
	if err := os.Chmod(filepath.Join(root, "memory.max"), 0o444); err != nil {
		t.Fatal(err)
	}
	var c calls
	m := setup(root, "", testHooks(&c, false))
	if m.mode != "own" || m.direct {
		t.Fatalf("mode %q direct %v", m.mode, m.direct)
	}
	if _, err := m.Apply(Request{Unlimited: true}); err != nil {
		t.Fatal(err)
	}
	if len(c.systemctl) != 1 || strings.Join(c.systemctl[0], " ") != "set-property --runtime daedalus-ptyd.service MemoryMax=infinity MemoryHigh=infinity MemorySwapMax=infinity" {
		t.Fatalf("systemctl calls %v", c.systemctl)
	}
}

func TestSharedGroupIsLeftAlone(t *testing.T) {
	root := fakeGroup(t, map[string]string{"cgroup.procs": "4242\n777\n"})
	var c calls
	m := setup(root, "", testHooks(&c, true))
	if m.mode != "" || m.Status().Supported {
		t.Fatal("a group with other programs in it must not be capped")
	}
	if _, err := m.Apply(Request{Percent: 50}); err == nil {
		t.Fatal("applying must fail with the reason")
	}
	if read(t, filepath.Join(root, "memory.max")) != "max" {
		t.Fatal("nothing may be written")
	}
	if _, err := os.Stat(filepath.Join(root, "daemon")); err == nil {
		t.Fatal("nothing may be created")
	}
}

func TestFailedSplitIsUndone(t *testing.T) {
	root := fakeGroup(t, nil)
	var c calls
	h := testHooks(&c, true)
	h.probe = func(string) error { return os.ErrPermission }
	m := setup(root, "", h)
	if m.mode != "own" || !strings.Contains(m.reason, "could not be split") {
		t.Fatalf("mode %q: %s", m.mode, m.reason)
	}
	// A kernel group is removed with its interface files; the scratch copy keeps them, so what is
	// checked is the order that lets the kernel take the daemon back: controllers off, then the move.
	if got := read(t, filepath.Join(root, "cgroup.subtree_control")); got != "-cpu -memory -pids" {
		t.Fatalf("root still hands %q to its children", got)
	}
	if got := read(t, filepath.Join(root, "cgroup.procs")); got != "4242" {
		t.Fatalf("the daemon must be back in its group: %q", got)
	}
}

func TestLower(t *testing.T) {
	for _, c := range [][3]int64{{none, none, none}, {none, 5, 5}, {5, none, 5}, {5, 3, 3}, {-1, 5, 5}, {5, -1, 5}, {-1, none, none}, {0, none, 0}, {none, -1, none}} {
		if got := lower(c[0], c[1]); got != c[2] {
			t.Fatalf("lower(%d, %d) = %d, want %d", c[0], c[1], got, c[2])
		}
	}
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

// A lift systemd refuses leaves the unit's limit in force; the cap on jobs is then kept under it by
// DaemonHeadroom, so the programs are killed at jobs before the group holding the daemon is full.
func TestRefusedLiftKeepsTheCapUnderTheUnitLimit(t *testing.T) {
	root := fakeGroup(t, map[string]string{"memory.max": "8589934592\n"})
	c := calls{refuse: true}
	m := setup(root, "", testHooks(&c, true))
	status, err := m.Apply(Request{Bytes: 16 << 30})
	if err != nil {
		t.Fatal(err)
	}
	if got := read(t, filepath.Join(root, "jobs", "memory.max")); got != itoa(8<<30-DaemonHeadroom) {
		t.Fatalf("jobs = %s", got)
	}
	if Bytes(status.MaxBytes) != 8<<30-DaemonHeadroom || !strings.Contains(status.Reason, "lower limit stays in force") {
		t.Fatalf("status %+v", status)
	}
}

// A percentage set with set-property lives in its own drop-in, which sorts after the absolute one and
// would come back at systemd's next reload: what is lifted loses its percentage drop-in, nothing else.
func TestLiftingRemovesThePercentageDropIns(t *testing.T) {
	root := fakeGroup(t, map[string]string{"memory.max": "40404582400\n", "memory.high": "33670488064\n", "memory.swap.max": "2147483648\n"})
	dropins := t.TempDir()
	for _, name := range []string{"50-MemoryMaxScale.conf", "50-MemoryHighScale.conf", "50-MemorySwapMax.conf"} {
		if err := os.WriteFile(filepath.Join(dropins, name), []byte("[Service]\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	c := calls{root: root, dropins: dropins}
	m := setup(root, "", testHooks(&c, true))
	if _, err := m.Apply(Request{Percent: 60}); err != nil {
		t.Fatal(err)
	}
	left, _ := os.ReadDir(dropins)
	if len(left) != 1 || left[0].Name() != "50-MemorySwapMax.conf" {
		t.Fatalf("drop-ins left: %v", left)
	}
}

// The daemon is never in a throttled group. On 2026-11-18 the unit's MemoryHigh=50% throttled the
// whole group, the daemon with the builds: a file stat took over ten seconds, the Files panel answered
// 503 and every host command hung until the daemon refused new ones. Whatever is asked, no group that
// holds the daemon (its leaf, or its own group when it could not be split) carries a throttle point,
// and the capped group carries none either.
func TestTheDaemonIsNeverThrottled(t *testing.T) {
	for _, delegated := range []bool{true, false} {
		root := fakeGroup(t, map[string]string{"memory.high": "33670488064\n"})
		c := calls{root: root}
		m := setup(root, "", testHooks(&c, delegated))
		if delegated {
			// At start, before any cap is sent.
			if got := read(t, filepath.Join(root, "memory.high")); got != "max" {
				t.Fatalf("the daemon's group is throttled at %s before the first cap", got)
			}
		}
		for _, r := range []Request{{Percent: 60}, {Bytes: 1 << 30}, {Percent: 100}, {Unlimited: true}} {
			if _, err := m.Apply(r); err != nil {
				t.Fatal(err)
			}
			if delegated {
				leaf := filepath.Join(root, "daemon")
				if got := read(t, filepath.Join(leaf, "cgroup.procs")); got != "4242" {
					t.Fatalf("the daemon is not in its leaf: %q", got)
				}
				if got := read(t, filepath.Join(root, "jobs", "memory.high")); got != "max" {
					t.Fatalf("%+v: jobs is throttled at %s", r, got)
				}
				if _, err := os.Stat(filepath.Join(leaf, "memory.high")); err == nil {
					t.Fatal("the daemon's leaf was given a limit")
				}
			}
			// The unit's group holds the daemon in both layouts: its throttle point goes.
			if got := read(t, filepath.Join(root, "memory.high")); got != "max" {
				t.Fatalf("delegated=%v %+v: the daemon's group is throttled at %s", delegated, r, got)
			}
			if status := m.Status(); status.HighBytes != nil {
				t.Fatalf("a throttle point is in force: %+v", status)
			}
		}
	}
}
