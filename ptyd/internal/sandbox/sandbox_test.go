//go:build unix

package sandbox

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// flagAt returns the index of the first "flag a b" triple, or -1.
func flagAt(argv []string, flag, a, b string) int {
	for i := 0; i+2 < len(argv); i++ {
		if argv[i] == flag && argv[i+1] == a && argv[i+2] == b {
			return i
		}
	}
	return -1
}

func TestWrapOrdersTheMounts(t *testing.T) {
	base := t.TempDir()
	base, _ = filepath.EvalSymlinks(base)
	project := filepath.Join(base, "project")
	home := filepath.Join(base, "home")
	run := filepath.Join(home, "run") // a writable folder that holds the daemon's directory
	launch := filepath.Join(run, "launches", "l1")
	for _, d := range []string{project, run, launch} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	plan, err := Wrap(Options{Bwrap: "/usr/bin/bwrap", Argv: []string{"/bin/bash", "-l"}, Cwd: project,
		Writable: []string{project, home, project + "/"}, Mask: []string{run}, Rebind: []Bind{{Path: launch}}})
	if err != nil {
		t.Fatal(err)
	}
	argv := plan.Argv
	if argv[0] != "/usr/bin/bwrap" || !slices.Equal(argv[len(argv)-5:], []string{"--chdir", project, "--", "/bin/bash", "-l"}) {
		t.Fatalf("argv: %q", argv)
	}
	if !slices.Equal(plan.Writable, []string{project, home}) {
		t.Fatalf("writable (a repeat is bound once): %q", plan.Writable)
	}
	if slices.Contains(argv, "--new-session") {
		t.Fatal("--new-session takes the terminal away from the shell, and job control with it")
	}
	root := flagAt(argv, "--ro-bind", "/", "/")
	pts := flagAt(argv, "--dev-bind", "/dev/pts", "/dev/pts")
	dev := slices.Index(argv, "--dev")
	tmp := flagAt(argv, "--tmpfs", "/tmp", "--unshare-pid")
	bindHome := flagAt(argv, "--bind", home, home)
	mask := slices.Index(argv, run)
	rebind := flagAt(argv, "--ro-bind", launch, launch)
	if root != 1 || dev < 0 || pts < dev || tmp < 0 || bindHome < tmp || mask < bindHome || rebind < mask {
		t.Fatalf("order root %d dev %d pts %d tmp %d home %d mask %d rebind %d: %q", root, dev, pts, tmp, bindHome, mask, rebind, argv)
	}
	if argv[mask-1] != "--tmpfs" {
		t.Fatalf("the daemon's directory is not masked: %q", argv)
	}
	if !slices.Contains(argv, "--die-with-parent") {
		t.Fatal("no --die-with-parent")
	}
}

func TestWrapLeavesUnsuitableFoldersReadOnly(t *testing.T) {
	base := t.TempDir()
	base, _ = filepath.EvalSymlinks(base)
	dir := filepath.Join(base, "dir")
	file := filepath.Join(base, "file")
	link := filepath.Join(base, "link")
	state := filepath.Join(base, "state")
	for _, d := range []string{dir, state, filepath.Join(state, "x")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(file, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	plan, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: dir, Mask: []string{state},
		Writable: []string{"/", "relative", "/proc/self", "/dev/shm", file, link, filepath.Join(base, "none"), state, filepath.Join(state, "x"), dir}})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"/": "the whole filesystem", "relative": "not an absolute path", "/proc/self": "a system directory",
		"/dev/shm": "a system directory", file: "not a directory", link: "a symbolic link", filepath.Join(base, "none"): "missing",
		state: "inside the terminal daemon's own directories", filepath.Join(state, "x"): "inside the terminal daemon's own directories"}
	if len(plan.Skipped) != len(want) {
		t.Fatalf("skipped %+v", plan.Skipped)
	}
	for _, s := range plan.Skipped {
		if want[s.Path] != s.Reason {
			t.Errorf("%s: %q, want %q", s.Path, s.Reason, want[s.Path])
		}
	}
	if !slices.Equal(plan.Writable, []string{dir}) {
		t.Fatalf("writable %q", plan.Writable)
	}
	for i := 0; i+1 < len(plan.Argv); i++ {
		if plan.Argv[i] == "--bind" && plan.Argv[i+1] != dir {
			t.Fatalf("bound %s", plan.Argv[i+1])
		}
	}
}

func TestStrictWrapRejectsWritableSymlinkParents(t *testing.T) {
	base := t.TempDir()
	base, _ = filepath.EvalSymlinks(base)
	dir := filepath.Join(base, "real", "project")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(base, "alias")
	if err := os.Symlink(filepath.Join(base, "real"), alias); err != nil {
		t.Fatal(err)
	}
	throughParent := filepath.Join(alias, "project")
	plan, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: dir,
		Writable: []string{throughParent, dir}, Strict: true})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(plan.Writable, []string{dir}) || len(plan.Skipped) != 1 ||
		plan.Skipped[0].Path != throughParent || plan.Skipped[0].Reason != "a symbolic link" {
		t.Fatalf("a parent symlink was writable: %+v", plan)
	}
	_, err = Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: dir,
		Rebind: []Bind{{Path: throughParent, Writable: true}}, Strict: true})
	if err == nil {
		t.Fatal("a writable daemon rebind through a parent symlink was accepted")
	}
}

func TestWrapWorkingDirectory(t *testing.T) {
	base := t.TempDir()
	base, _ = filepath.EvalSymlinks(base)
	state := filepath.Join(base, "state")
	if err := os.MkdirAll(filepath.Join(state, "inner"), 0o755); err != nil {
		t.Fatal(err)
	}
	// Inside the daemon's directories: refused, never an empty stand-in.
	_, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: filepath.Join(state, "inner"), Mask: []string{state}})
	if !errors.Is(err, ErrHiddenCwd) {
		t.Fatalf("hidden cwd: %v", err)
	}
	if _, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: "rel"}); err == nil {
		t.Fatal("a relative cwd was accepted")
	}
	// In /tmp and not writable: shown again read-only over the private /tmp. t.TempDir is under
	// /tmp on Linux; elsewhere the case does not arise.
	if !strings.HasPrefix(base, "/tmp/") {
		t.Skip("the temporary directory is not under /tmp here")
	}
	plan, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: base})
	if err != nil {
		t.Fatal(err)
	}
	if flagAt(plan.Argv, "--ro-bind", base, base) < 0 {
		t.Fatalf("cwd in /tmp not shown: %q", plan.Argv)
	}
	plan, _ = Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: base, Writable: []string{base}})
	if flagAt(plan.Argv, "--ro-bind", base, base) >= 0 || flagAt(plan.Argv, "--bind", base, base) < 0 {
		t.Fatalf("cwd in a writable folder bound read-only: %q", plan.Argv)
	}
}

func TestWrapRebindsExistingPathsOnly(t *testing.T) {
	base := t.TempDir()
	dial := filepath.Join(base, "dial")
	if err := os.Mkdir(dial, 0o700); err != nil {
		t.Fatal(err)
	}
	plan, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: "/",
		Rebind: []Bind{{Path: dial, Writable: true}, {Path: filepath.Join(base, "gone")}, {Path: ""}}})
	if err != nil {
		t.Fatal(err)
	}
	if flagAt(plan.Argv, "--bind", dial, dial) < 0 || slices.Contains(plan.Argv, filepath.Join(base, "gone")) {
		t.Fatalf("rebinds: %q", plan.Argv)
	}
	if _, err := Wrap(Options{Bwrap: "bwrap", Argv: []string{"/bin/sh"}, Cwd: "/", Writable: make([]string, MaxWritable+1)}); err == nil {
		t.Fatal("too many writable folders accepted")
	}
}

func TestProberCachesSuccessForGoodAndFailureForAWhile(t *testing.T) {
	var calls atomic.Int32
	answer := atomic.Bool{}
	run := func(_ context.Context, argv []string) (string, bool) {
		calls.Add(1)
		if !slices.Equal(argv, []string{"/b/bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "true"}) {
			t.Errorf("probe argv %q", argv)
		}
		if answer.Load() {
			return "", true
		}
		return "bwrap: setting up uid map: Permission denied\n", false
	}
	now := time.Unix(1000, 0)
	var clock atomic.Int64
	clock.Store(now.UnixNano())
	p := NewTestProber("/b/bwrap", "linux", run, func() time.Time { return time.Unix(0, clock.Load()) })
	ctx := context.Background()
	s := p.Status(ctx)
	if !strings.HasPrefix(s, "bwrap cannot create namespaces here: bwrap: setting up uid map: Permission denied (") {
		t.Fatalf("status %q", s)
	}
	answer.Store(true)
	if p.Status(ctx) != s || calls.Load() != 1 {
		t.Fatalf("a failure was not cached: %d calls", calls.Load())
	}
	clock.Add(int64(RetryAfter))
	if p.Status(ctx) != OK || calls.Load() != 2 {
		t.Fatalf("a stale failure was not probed again: %d calls", calls.Load())
	}
	clock.Add(int64(24 * time.Hour))
	if p.Status(ctx) != OK || calls.Load() != 2 {
		t.Fatalf("success was probed again: %d calls", calls.Load())
	}
}

func TestProberPeekDoesNotWait(t *testing.T) {
	release := make(chan struct{})
	p := NewTestProber("/b/bwrap", "linux", func(context.Context, []string) (string, bool) {
		<-release
		return "", true
	}, time.Now)
	if s := p.Peek(); s != "not checked yet" {
		t.Fatalf("peek before the first probe: %q", s)
	}
	close(release)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if s := p.Status(ctx); s != OK {
		t.Fatalf("status: %q", s)
	}
	if s := p.Peek(); s != OK {
		t.Fatalf("peek after: %q", s)
	}
}

func TestProberElsewhere(t *testing.T) {
	never := func(context.Context, []string) (string, bool) { t.Error("probed"); return "", true }
	if s := NewTestProber("/b/bwrap", "windows", never, time.Now).Status(context.Background()); s != "not available on Windows" {
		t.Fatalf("windows: %q", s)
	}
	if s := NewTestProber("", "linux", never, time.Now).Status(context.Background()); s != "bwrap is not installed" {
		t.Fatalf("missing: %q", s)
	}
}
