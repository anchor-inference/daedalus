package ax_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/ax"
	"github.com/ascorblack/daedalus/deskd/internal/ax/axfake"
	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

// childEnvName makes the test binary act as the fake tool when the package runs it as its child.
const childEnvName = "DESKD_AX_TEST_CHILD"

func TestMain(m *testing.M) {
	if os.Getenv(childEnvName) == "1" {
		os.Exit(axfake.Main(os.Args[1:], os.Stdout))
	}
	os.Exit(m.Run())
}

// fakeTool is the test binary run as the fake tool, over scene.
func fakeTool(t *testing.T, scene axfake.Scene) (ax.Config, string) {
	t.Helper()
	dir := t.TempDir()
	if scene.Log == "" {
		scene.Log = filepath.Join(dir, "log.jsonl")
	}
	b, _ := json.Marshal(scene)
	sceneFile := filepath.Join(dir, "scene.json")
	if err := os.WriteFile(sceneFile, b, 0o600); err != nil {
		t.Fatal(err)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return ax.Config{Path: self, StateDir: filepath.Join(dir, "state"),
		Env: []string{childEnvName + "=1", axfake.SceneEnv + "=" + sceneFile, "GORACE=atexit_sleep_ms=0"}}, scene.Log
}

func readLog(t *testing.T, path string) []axfake.LogLine {
	t.Helper()
	b, _ := os.ReadFile(path)
	var out []axfake.LogLine
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		var l axfake.LogLine
		if json.Unmarshal([]byte(line), &l) == nil {
			out = append(out, l)
		}
	}
	return out
}

func TestOnlyTheEnvelopeTheDaemonReadsIsSpokenTo(t *testing.T) {
	for v, want := range map[string]bool{"2.5": true, "2.9": true, "2.4": false, "3.0": false, "2": false} {
		if got := ax.EnvelopeSupported(v); got != want {
			t.Errorf("envelope %q: %v", v, got)
		}
	}
}

func TestAnotherVersionOrAMissingOrBrokenBinaryIsNotServed(t *testing.T) {
	ctx := context.Background()
	cfg, _ := fakeTool(t, axfake.Scene{})
	if st := ax.Open(ctx, cfg).Status(); !st.Available || st.Version != "1.0.2" {
		t.Fatalf("the fake tool: %+v", st)
	}
	for _, v := range []string{"1.0.1", "1.0.3", "1.1.0", "2.0.0"} {
		other, _ := fakeTool(t, axfake.Scene{Version: v})
		if st := ax.Open(ctx, other).Status(); st.Available || st.Reason != ax.ReasonVersion || st.Version != v {
			t.Fatalf("tool %s beside a daemon pinned to %s: %+v", v, ax.PinnedVersion, st)
		}
	}
	oldEnvelope, _ := fakeTool(t, axfake.Scene{Envelope: "2.4"})
	if st := ax.Open(ctx, oldEnvelope).Status(); st.Available || st.Reason != ax.ReasonVersion {
		t.Fatalf("an older envelope: %+v", st)
	}
	missing := cfg
	missing.Path = filepath.Join(t.TempDir(), "agent-desktop")
	if st := ax.Open(ctx, missing).Status(); st.Available || st.Reason != ax.ReasonMissing {
		t.Fatalf("no binary: %+v", st)
	}
	notABinary := cfg
	notABinary.Path = filepath.Join(t.TempDir(), "agent-desktop")
	_ = os.WriteFile(notABinary.Path, []byte("not a program"), 0o700)
	if st := ax.Open(ctx, notABinary).Status(); st.Available || st.Reason != ax.ReasonFailed {
		t.Fatalf("a binary that does not run here: %+v", st)
	}
	if runtime.GOOS != "windows" {
		// What the dynamic loader does with a binary built for a newer C library than the system's:
		// a line on stderr, exit 1, nothing on stdout.
		oldLibc := cfg
		oldLibc.Path = filepath.Join(t.TempDir(), "agent-desktop")
		script := "#!/bin/sh\necho \"version 'GLIBC_2.35' not found\" >&2\nexit 1\n"
		_ = os.WriteFile(oldLibc.Path, []byte(script), 0o700)
		if st := ax.Open(ctx, oldLibc).Status(); st.Available || st.Reason != ax.ReasonFailed {
			t.Fatalf("a binary the C library refuses: %+v", st)
		}
	}
	if _, err := ax.Off(ax.ReasonOff).Run(ctx, "l1", time.Second, "version"); !isUnavailable(err, ax.ReasonOff) {
		t.Fatalf("a tool that is off: %v", err)
	}
}

func isUnavailable(err error, reason string) bool {
	var un *ax.Unavailable
	return errors.As(err, &un) && un.Reason == reason
}

func TestACrashOrAnAnswerThatIsNotJSONIsTheToolUnavailableForThatCall(t *testing.T) {
	ctx := context.Background()
	for name, scene := range map[string]axfake.Scene{"crash": {Crash: true}, "garbage": {Garbage: true}} {
		cfg, _ := fakeTool(t, scene)
		tool := ax.Open(ctx, cfg)
		if !tool.Status().Available {
			t.Fatalf("%s: version should still answer: %+v", name, tool.Status())
		}
		if _, err := tool.Run(ctx, "l1", time.Second, ax.ListWindowsArgs()...); !isUnavailable(err, ax.ReasonCrashed) {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

func TestAChildPastItsDeadlineOrItsCallIsKilled(t *testing.T) {
	saved := ax.SetKillGrace(200 * time.Millisecond)
	t.Cleanup(func() { ax.SetKillGrace(saved) })
	ctx := context.Background()
	cfg, _ := fakeTool(t, axfake.Scene{HangMs: 30000})
	tool := ax.Open(ctx, cfg)
	start := time.Now()
	if _, err := tool.Run(ctx, "l1", 100*time.Millisecond, ax.ListWindowsArgs()...); !errors.Is(err, ax.ErrDeadline) {
		t.Fatalf("a hung child: %v", err)
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("the hung child lived %v", d)
	}
	cctx, cancel := context.WithCancel(ctx)
	go func() { time.Sleep(100 * time.Millisecond); cancel() }()
	start = time.Now()
	if _, err := tool.Run(cctx, "l1", 20*time.Second, ax.ListWindowsArgs()...); !errors.Is(err, context.Canceled) {
		t.Fatalf("a cancelled call: %v", err)
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("the cancelled child lived %v", d)
	}
}

func TestTheChildSeesNoneOfTheToolsVariablesButItsOwnStateRoot(t *testing.T) {
	t.Setenv("AGENT_DESKTOP_SESSION", "elsewhere")
	t.Setenv("AGENT_DESKTOP_HOME", "/somewhere/else")
	t.Setenv("AGENT_DESKTOP_INTERNAL_COMMAND_HOST", "1")
	ctx := context.Background()
	cfg, log := fakeTool(t, axfake.Scene{})
	tool := ax.Open(ctx, cfg)
	if _, err := tool.Run(ctx, "l1", time.Second, ax.ListWindowsArgs()...); err != nil {
		t.Fatal(err)
	}
	lines := readLog(t, log)
	last := lines[len(lines)-1]
	if !slices.Equal(last.Env, []string{"AGENT_DESKTOP_HOME"}) || last.Home != filepath.Join(cfg.StateDir, "l1") {
		t.Fatalf("the child's environment: %+v", last)
	}
	st, err := os.Stat(last.Home)
	if err != nil || st.Mode().Perm() != 0o700 {
		t.Fatalf("the state root: %v %v", st, err)
	}
	tool.EndLease("l1")
	if _, err := os.Stat(last.Home); !os.IsNotExist(err) {
		t.Fatalf("the state root outlived its lease: %v", err)
	}
	// A state root left by an earlier run of the daemon is emptied at start.
	stray := filepath.Join(cfg.StateDir, "l0", "snapshots")
	_ = os.MkdirAll(stray, 0o700)
	ax.Open(ctx, cfg)
	if _, err := os.Stat(stray); !os.IsNotExist(err) {
		t.Fatalf("an earlier run's state survived: %v", err)
	}
}

func TestEveryValueFromTheHostComesAfterTheEndOfTheFlags(t *testing.T) {
	args, err := ax.ActArgs(ax.ActType, "@s1:e2", "--headed", "", 0, 5000)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(args, []string{"type", "--timeout-ms", "5000", "--", "@s1:e2", "--headed"}) {
		t.Fatalf("type: %q", args)
	}
	args, _ = ax.ActArgs(ax.ActScroll, "@s1:e2", "", "up", 4, 5000)
	if !slices.Equal(args, []string{"scroll", "--timeout-ms", "5000", "--direction", "up", "--amount", "4", "--", "@s1:e2"}) {
		t.Fatalf("scroll: %q", args)
	}
	find := ax.FindArgs{Window: "w-1", Name: "--force", Role: "button", Limit: 5, TimeoutMs: 5000}.Args()
	if !slices.Contains(find, "--name=--force") || slices.Contains(find, "--force") {
		t.Fatalf("find: %q", find)
	}
	if got := ax.GetArgs("@s1:e2", "checked"); !slices.Equal(got, []string{"is", "--property", "checked", "--", "@s1:e2"}) {
		t.Fatalf("is: %q", got)
	}
	for _, all := range [][]string{args, find, ax.GetArgs("@s1:e2", "text"),
		ax.SnapshotArgs{Window: "w-1", Interactive: true, Skeleton: true, Bounds: true, TimeoutMs: 3000}.Args()} {
		for _, a := range all {
			if a == "--headed" || a == "--force" {
				t.Fatalf("a real-input flag in %q", all)
			}
		}
	}
	if _, err := ax.ActArgs("press", "@s1:e2", "", "", 0, 5000); err == nil {
		t.Fatal("press is not an element action")
	}
	for _, ref := range []string{"@s1:e2", "@abcDEF09:e1234567"} {
		if _, ok := ax.SnapshotOf(ref); !ok {
			t.Errorf("%q", ref)
		}
	}
	for _, ref := range []string{"--headed", "@s1:e", "s1:e2", "@s1:e2 --headed", "@s-1:e2", "@:e2", "@s1:x2"} {
		if _, ok := ax.SnapshotOf(ref); ok {
			t.Errorf("%q passed as a reference", ref)
		}
	}
}

func TestWindowsAndMacOSWindowsMapDirectly(t *testing.T) {
	if id, err := ax.DirectID("windows", "0x1A2b"); err != nil || id != "w-6699" {
		t.Fatalf("windows: %q %v", id, err)
	}
	if id, ok := ax.FromDirectID("windows", "w-6699"); !ok || id != "0x1a2b" {
		t.Fatalf("windows back: %q", id)
	}
	if id, err := ax.DirectID("darwin", "4521"); err != nil || id != "w-4521" {
		t.Fatalf("darwin: %q %v", id, err)
	}
	if id, ok := ax.FromDirectID("darwin", "w-4521"); !ok || id != "4521" {
		t.Fatalf("darwin back: %q", id)
	}
	for _, bad := range []string{"1a2b", "0xzz", ""} {
		if _, err := ax.DirectID("windows", bad); err == nil {
			t.Errorf("windows %q", bad)
		}
	}
	if _, ok := ax.FromDirectID("windows", "w-1-org-a11y"); ok {
		t.Error("an AT-SPI id read as a handle")
	}
	if ax.MappingFor("windows") != ax.MapDirect || ax.MappingFor("darwin") != ax.MapDirect || ax.MappingFor("linux") != ax.MapOverlap ||
		ax.MappingFor("fake") != ax.MapOverlap {
		t.Fatal("mappings")
	}
}

func TestALinuxWindowIsMatchedByItsProcessAndWhereItIs(t *testing.T) {
	frame := geom.Rect{X: 100, Y: 100, W: 800, H: 600}
	client := &ax.ToolRect{X: 100, Y: 130, Width: 800, Height: 570}
	dialog := &ax.ToolRect{X: 300, Y: 300, Width: 300, Height: 150}
	ws := []ax.ToolWindow{
		{ID: "w-1-dialog", PID: 7, Bounds: dialog},
		{ID: "w-1-main", PID: 7, Bounds: client},
		{ID: "w-2-other", PID: 8, Bounds: client},
		{ID: "w-1-nobounds", PID: 7},
	}
	if m := ax.MatchByOverlap(ws, 7, frame); m.ID != "w-1-main" || m.Ambiguous {
		t.Fatalf("the main window: %+v", m)
	}
	if m := ax.MatchByOverlap(ws, 9, frame); m.ID != "" {
		t.Fatalf("another process: %+v", m)
	}
	if m := ax.MatchByOverlap(ws, 0, frame); m.ID != "" {
		t.Fatalf("a window with no process: %+v", m)
	}
	twins := append(ws, ax.ToolWindow{ID: "w-1-twin", PID: 7, Bounds: client})
	if m := ax.MatchByOverlap(twins, 7, frame); !m.Ambiguous || m.ID != "" {
		t.Fatalf("two windows in one place: %+v", m)
	}
	if m := ax.MatchByOverlap([]ax.ToolWindow{{ID: "w-1-nobounds", PID: 7}}, 7, frame); m.ID != "" {
		t.Fatalf("a window with no bounds: %+v", m)
	}
}

func TestASecureFieldNeverCarriesItsValue(t *testing.T) {
	pw, name := "hunter2", "Password"
	refs := map[string]ax.RefInfo{}
	n := ax.Convert(ax.ToolNode{Role: "window", Children: []ax.ToolNode{
		{RefID: "@s1:e1", Role: "textfield", Name: &name, Value: &pw, States: []string{"secure"}},
		{RefID: "@s1:e2", Role: "PasswordField", Value: &pw},
		{RefID: "@s1:e3", Role: "button", Name: &name},
	}}, refs)
	for _, c := range n.Children[:2] {
		if c.Value != nil || !c.Secure {
			t.Fatalf("a secure field came out with its value: %+v", c)
		}
	}
	if !refs["@s1:e1"].Secure || !refs["@s1:e2"].Secure || refs["@s1:e3"].Secure || refs["@s1:e3"].Role != "button" {
		t.Fatalf("the references: %+v", refs)
	}
	post := (&ax.ToolPostState{Role: "textfield", Value: &pw}).Post(true)
	if post.Value != nil {
		t.Fatal("a read-back of a secure field carried its value")
	}
}
