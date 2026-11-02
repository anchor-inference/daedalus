//go:build unix

package term

import (
	"bytes"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
	"github.com/ascorblack/daedalus/ptyd/internal/scan/scantest"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
)

// heapInUse is the live heap after a collection.
func heapInUse() uint64 {
	runtime.GC()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return m.HeapInuse
}

// peakRSS returns the process's peak resident memory since the last resetPeakRSS, in bytes, or 0
// where /proc cannot tell. The emulator's memory is C memory, which the Go heap figures do not see.
func peakRSS() int64 {
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if rest, ok := strings.CutPrefix(line, "VmHWM:"); ok {
			kb, _ := strconv.ParseInt(strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(rest), "kB")), 10, 64)
			return kb << 10
		}
	}
	return 0
}

// resetPeakRSS starts a new peak (Linux: writing 5 to clear_refs resets VmHWM to the current RSS).
func resetPeakRSS() { _ = os.WriteFile("/proc/self/clear_refs", []byte("5"), 0) }

// catThrough runs `cat path` in a terminal with the production emulator and answerer, and returns
// the output held in the ring, the time it took, the heap growth it left behind, and the peak
// resident memory above where it started.
func catThrough(t *testing.T, reg *Registry, id, path string, ringBytes int) ([]byte, time.Duration, int64, int64) {
	t.Helper()
	before := heapInUse()
	resetPeakRSS()
	baseRSS := peakRSS()
	env := procenv.BuildEnv(os.Environ(), nil, nil, id)
	cat, ok := procenv.LookPath("cat", env, "/")
	if !ok {
		t.Skip("no cat")
	}
	began := time.Now()
	term, err := reg.Create(Spec{ID: id, Path: cat, Argv: []string{"cat", path}, Cwd: "/", Env: env, Cols: 120,
		Rows: 40, RingBytes: ringBytes})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-term.Done():
	case <-time.After(60 * time.Second):
		t.Fatalf("%s: no exit within a minute", id)
	}
	took := time.Since(began)
	// Everything the terminal read is in the ring's last 64 KiB after exit; read what is left.
	data, _, _, _ := term.ReadOutput(0, 1<<20, false)
	if err := reg.Forget(id); err != nil {
		t.Fatal(err)
	}
	return data, took, int64(heapInUse()) - int64(before), peakRSS() - baseRSS
}

// countedEvents is the daemon's own event pipeline, its per-terminal limits and its bounded log,
// with a count of what was offered to it by kind. What the heap keeps is then what the daemon keeps:
// a recorder of every event would itself grow with a stream of bells, which is what macOS turns
// unread queries into (the terminal rings once for every byte its full input queue drops), where
// Linux holds the writer back instead.
type countedEvents struct {
	inner Publisher
	mu    sync.Mutex
	n     map[string]int
}

func (c *countedEvents) Publish(typ, id string, data any) {
	c.mu.Lock()
	c.n[typ]++
	c.mu.Unlock()
	c.inner.Publish(typ, id, data)
}

func (c *countedEvents) Flush(id string) { c.inner.Flush(id) }

// take returns the counts since the last call.
func (c *countedEvents) take() map[string]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := c.n
	c.n = map[string]int{}
	return n
}

func adversarialRegistry(t *testing.T) (*Registry, *countedEvents) {
	evlog := events.NewLog(config.EventRingSize)
	evlog.SetMaxBytes(config.EventLogBytes)
	counted := &countedEvents{inner: events.NewDebouncer(evlog, events.Policies), n: map[string]int{}}
	reg := NewRegistry(Deps{
		Emulator: production.Factory, Answer: answer.Reply, Events: counted, Clock: RealClock{},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), KillGrace: 100 * time.Millisecond,
	}, 4)
	t.Cleanup(func() { reg.Shutdown(100 * time.Millisecond) })
	return reg, counted
}

// TestHostileOutputIsBounded sends the kinds of output that made emulators allocate without limit
// through a real PTY, at sizes a test run can afford (about 12 MB in all), and checks that what the
// daemon keeps and passes on is clamped and that its heap did not grow with the input.
func TestHostileOutputIsBounded(t *testing.T) {
	dir := t.TempDir()
	rep := func(s string, n int) string { return strings.Repeat(s, n) }
	inputs := map[string]string{
		"unterminated-osc": "\x1b]0;" + rep("a", 6<<20) + "\x07END",
		"rep-and-scroll":   rep("x\x1b[1000000000b\x1b[100000000S\x1b[4000000000;4000000000H", 2000) + "END",
		"wide-sgr":         "\x1b[" + rep("1;", 200000) + "1mEND",
		"endless-dcs":      "\x1bPq" + rep("#0;2;0;0;0", 300000) + "\x1b\\END",
		"title-stack":      "\x1b]2;" + rep("t", 1<<20) + "\x07" + rep("\x1b[22;0t", 20000) + "END",
		// Queries nobody reads the answers to: cat reads its file, never the terminal.
		"unread-queries": rep("\x1b[6n\x1b[c\x1b]11;?\x07", 200000) + "END",
		"kitty-stack":    rep("\x1b[>1u", 100000) + rep("\x1b[<u", 100000) + "END",
		"unique-links": func() string {
			var b strings.Builder
			for i := 0; i < 50000; i++ {
				b.WriteString("\x1b]8;;http://x/" + strconv.Itoa(i) + "\x1b\\l\x1b]8;;\x1b\\")
			}
			return b.String() + "END"
		}(),
	}
	reg, counted := adversarialRegistry(t)
	names := make([]string, 0, len(inputs))
	for name := range inputs {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(inputs[name]), 0o600); err != nil {
			t.Fatal(err)
		}
		out, took, growth, rss := catThrough(t, reg, name, path, 1<<20)
		// The terminal echoes the answers cat never reads, megabytes of them, and the echo outlasts
		// the file: there the exit within the minute is the check.
		if name != "unread-queries" && !bytes.HasSuffix(out, []byte("END")) {
			t.Errorf("%s: the text after the hostile part was lost (%d bytes kept, ending %q)", name, len(out), out[max(0, len(out)-200):])
		}
		if err := scantest.Verify(out); err != nil {
			t.Errorf("%s: %v", name, err)
		}
		if growth > 32<<20 {
			t.Errorf("%s: the heap grew by %d MB", name, growth>>20)
		}
		// Ghostty held at most 92 MB on any unclamped probe; the clamped stream through one terminal
		// must stay well inside that.
		if rss > 128<<20 {
			t.Errorf("%s: resident memory peaked %d MB above the start", name, rss>>20)
		}
		t.Logf("%-18s %5d KB in, %6s, heap %+d KB, peak RSS %+d KB, events %v", name, len(inputs[name])>>10,
			took.Round(time.Millisecond), growth>>10, rss>>10, counted.take())
	}
}

// TestProbeCorpus feeds every file of an external corpus of unclamped probes (up to tens of
// megabytes each) through a terminal. It runs only when PTYD_PROBES_DIR names such a directory, and
// only under a memory cap: an unclamped probe is exactly what took a machine down once.
func TestProbeCorpus(t *testing.T) {
	dir := os.Getenv("PTYD_PROBES_DIR")
	if dir == "" {
		t.Skip("PTYD_PROBES_DIR is not set")
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.bin"))
	if len(files) == 0 {
		t.Fatalf("no probes in %s", dir)
	}
	reg, _ := adversarialRegistry(t)
	var peak, peakRSSAbove int64
	var slowest time.Duration
	for i, path := range files {
		st, _ := os.Stat(path)
		out, took, growth, rss := catThrough(t, reg, "probe"+strconv.Itoa(i), path, 8<<20)
		if err := scantest.Verify(out); err != nil {
			t.Errorf("%s: %v", filepath.Base(path), err)
		}
		if rss > 256<<20 {
			t.Errorf("%s: resident memory peaked %d MB above the start", filepath.Base(path), rss>>20)
		}
		peak, peakRSSAbove, slowest = max(peak, growth), max(peakRSSAbove, rss), max(slowest, took)
		t.Logf("%-28s %7d KB in, %7s, %6d KB kept, heap %+d KB, peak RSS %+d KB", filepath.Base(path), st.Size()>>10,
			took.Round(time.Millisecond), len(out)>>10, growth>>10, rss>>10)
	}
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	t.Logf("largest heap growth %d KB; largest peak RSS above start %d MB; slowest %s; heap system %d MB",
		peak>>10, peakRSSAbove>>20, slowest.Round(time.Millisecond), m.HeapSys>>20)
}

// throughputScript is the output the throughput benchmarks run: ordinary coloured build lines, or
// with PTYD_BENCH_SHORT_LINES set, `yes` itself (two-byte lines, the worst case per byte: every line
// is a scroll).
func throughputScript(total int) []string {
	line := `"$(printf '\033[32mok\033[0m  build step with a path /usr/lib/x.so and some words')"`
	if os.Getenv("PTYD_BENCH_SHORT_LINES") != "" {
		line = ""
	}
	return []string{"sh", "-c", `yes ` + line + ` | head -c ` + strconv.Itoa(total)}
}

// BenchmarkPTYAlone is the floor the daemon is measured against: the same program and PTY with the
// output read and thrown away, no scanner, ring or emulator.
func BenchmarkPTYAlone(b *testing.B) {
	const total = 100_000_000
	env := procenv.BuildEnv(os.Environ(), nil, nil, "bench")
	sh, _ := procenv.LookPath("sh", env, "/")
	b.SetBytes(total)
	buf := make([]byte, readBytes)
	for i := 0; i < b.N; i++ {
		argv := throughputScript(total)
		p, err := ptyproc.Start(ptyproc.Spec{Path: sh, Argv: argv, Dir: "/", Env: env, Cols: 120, Rows: 40})
		if err != nil {
			b.Fatal(err)
		}
		n := 0
		for {
			k, err := p.Master.Read(buf)
			n += k
			if err != nil {
				break
			}
		}
		p.Wait()
		_ = p.Close()
		if n < total {
			b.Fatalf("only %d bytes arrived", n)
		}
	}
}

// BenchmarkThroughput measures how fast output goes from a program through the PTY, the scanner,
// the ring and the production emulator: 100 MB of ordinary coloured lines per iteration.
func BenchmarkThroughput(b *testing.B) {
	reg := NewRegistry(Deps{
		Emulator: production.Factory, Answer: answer.Reply, Events: &recorder{}, Clock: RealClock{},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)), KillGrace: 100 * time.Millisecond,
	}, 4)
	defer reg.Shutdown(100 * time.Millisecond)
	const total = 100_000_000
	env := procenv.BuildEnv(os.Environ(), nil, nil, "bench")
	sh, _ := procenv.LookPath("sh", env, "/")
	b.SetBytes(total)
	for i := 0; i < b.N; i++ {
		id := "bench" + strconv.Itoa(i)
		term, err := reg.Create(Spec{ID: id, Path: sh, Argv: throughputScript(total),
			Cwd: "/", Env: env, Cols: 120, Rows: 40, RingBytes: 8 << 20})
		if err != nil {
			b.Fatal(err)
		}
		<-term.Done()
		if got := term.OutputHead(); got < total {
			b.Fatalf("only %d bytes arrived", got)
		}
		_ = reg.Forget(id)
	}
}
