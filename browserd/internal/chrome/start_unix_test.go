//go:build unix

package chrome

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"
)

// fakeEnv names how the test binary, run as a browser, behaves.
const fakeEnv = "BROWSERD_FAKE_CHROMIUM"

// TestMain lets the test binary stand in for Chromium: started with fakeEnv set, it is a browser that
// exits at once, says nothing for ever, or answers on its pipe after a cold start's delay.
func TestMain(m *testing.M) {
	switch os.Getenv(fakeEnv) {
	case "":
		os.Exit(m.Run())
	case "exit":
		fmt.Fprintln(os.Stderr, "[0101/000000.000000:WARNING:a warning every start prints]")
		fmt.Fprintln(os.Stderr, "chrome: error while loading shared libraries: libnss3.so: cannot open shared object file")
		os.Exit(127)
	case "silent":
		fmt.Fprintln(os.Stderr, "still paging the binary in")
		time.Sleep(time.Hour)
	case "slow":
		time.Sleep(1500 * time.Millisecond)
		serveFake()
	}
	os.Exit(2)
}

// serveFake answers every command on the debugging pipe (fd 3 in, fd 4 out) until it ends.
func serveFake() {
	in, out := os.NewFile(3, "in"), os.NewFile(4, "out")
	r := bufio.NewReader(in)
	for {
		msg, err := r.ReadBytes(0)
		if err != nil {
			os.Exit(0)
		}
		var call struct {
			ID     int64  `json:"id"`
			Method string `json:"method"`
		}
		if json.Unmarshal(msg[:len(msg)-1], &call) != nil {
			continue
		}
		result := map[string]any{}
		if call.Method == "Browser.getVersion" {
			result = map[string]any{"product": "HeadlessChrome/1.0.0.0", "userAgent": "Mozilla/5.0 HeadlessChrome/1.0.0.0"}
		}
		reply, _ := json.Marshal(map[string]any{"id": call.ID, "result": result})
		out.Write(append(reply, 0))
	}
}

func startFake(t *testing.T, mode string) *Process {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	p, err := Start(Options{Path: self, ProfileDir: t.TempDir(), Env: append(os.Environ(), fakeEnv+"="+mode)}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.Kill)
	return p
}

// A browser that cannot start is reported at once, with its exit code and what it printed, however
// long the budget: before, it was only ever "context deadline exceeded".
func TestAnEarlyExitIsReportedWithItsStderr(t *testing.T) {
	p := startFake(t, "exit")
	begin := time.Now()
	err := p.FirstAnswer(context.Background(), time.Minute, "Browser.getVersion", nil)
	var se *StartError
	if !errors.As(err, &se) {
		t.Fatalf("not a StartError: %v", err)
	}
	if took := time.Since(begin); took > 10*time.Second {
		t.Fatalf("an exited browser was waited for %s", took)
	}
	if !se.Exited || se.ExitCode != 127 || !strings.Contains(se.Stderr, "libnss3.so") {
		t.Fatalf("%+v", se)
	}
	if msg := err.Error(); strings.Contains(msg, "deadline") || !strings.Contains(msg, "exited with code 127") ||
		!strings.Contains(msg, "libnss3.so") {
		t.Fatalf("message: %s", msg)
	}
}

// A browser that runs on without answering is reported when its budget ends, as still running, with
// what it printed meanwhile.
func TestASilentBrowserIsReportedWhenItsBudgetEnds(t *testing.T) {
	p := startFake(t, "silent")
	err := p.FirstAnswer(context.Background(), 500*time.Millisecond, "Browser.getVersion", nil)
	var se *StartError
	if !errors.As(err, &se) {
		t.Fatalf("not a StartError: %v", err)
	}
	if se.Exited || se.Pid != p.Pid || se.Waited < 500*time.Millisecond || !strings.Contains(se.Stderr, "still paging") {
		t.Fatalf("%+v", se)
	}
	if msg := err.Error(); !strings.Contains(msg, "did not answer Browser.getVersion on its pipe within") {
		t.Fatalf("message: %s", msg)
	}
}

// A start slower than a warm one but inside the budget is simply answered: the first start on a cold
// disk is slow, not stuck.
func TestASlowStartInsideTheBudgetIsAnswered(t *testing.T) {
	p := startFake(t, "slow")
	var ver struct {
		Product string `json:"product"`
	}
	if err := p.FirstAnswer(context.Background(), 20*time.Second, "Browser.getVersion", &ver); err != nil {
		t.Fatal(err)
	}
	if ver.Product != "HeadlessChrome/1.0.0.0" {
		t.Fatalf("version: %+v", ver)
	}
}

// The caller giving up is the caller's error, not the browser's.
func TestACancelledCallerIsNotBlamedOnTheBrowser(t *testing.T) {
	p := startFake(t, "silent")
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(200*time.Millisecond, cancel)
	err := p.FirstAnswer(ctx, time.Minute, "Browser.getVersion", nil)
	var se *StartError
	if errors.As(err, &se) || !errors.Is(err, context.Canceled) {
		t.Fatalf("%v", err)
	}
}

func TestLastLines(t *testing.T) {
	if got := lastLines("a\nb\nc\nd\n", 2, 100); got != "c\nd" {
		t.Fatalf("%q", got)
	}
	if got := lastLines(strings.Repeat("x", 50), 6, 10); got != "…"+strings.Repeat("x", 10) {
		t.Fatalf("%q", got)
	}
}
