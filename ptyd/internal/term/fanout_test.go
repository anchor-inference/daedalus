//go:build unix

package term

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/answer"
	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/emulator/production"
	"github.com/ascorblack/daedalus/ptyd/internal/procenv"
	"github.com/ascorblack/daedalus/ptyd/internal/wire"
)

// countingSink is a browser reduced to what a fan-out costs: it counts the output it is sent and,
// unless stalled, acknowledges it at once from its own goroutine, as a page that keeps up does. It
// keeps no frames, so a hundred megabytes through three of them measures the daemon, not the test.
type countingSink struct {
	stalled  bool
	received atomic.Int64
	acks     chan int64
}

func newCountingSink(stalled bool) *countingSink {
	return &countingSink{stalled: stalled, acks: make(chan int64, 1024)}
}

func (s *countingSink) Send(frame []byte) error {
	if len(frame) > 9 && frame[0] == wire.TypeOutput {
		f, err := wire.DecodeBrowser(frame)
		if err != nil {
			return err
		}
		end := int64(f.Seq) + int64(len(f.Data))
		s.received.Store(end)
		if !s.stalled {
			select {
			case s.acks <- end:
			default:
			}
		}
	}
	return nil
}

func (s *countingSink) Close() {}

func (s *countingSink) acknowledge(c *Client, done <-chan struct{}) {
	for {
		select {
		case end := <-s.acks:
			c.Frame(wire.EncodeAck(uint64(end)))
		case <-done:
			return
		}
	}
}

// BenchmarkFanOut is BenchmarkThroughput with three browsers attached: two that keep up and one that
// never acknowledges. The program must run as fast as with none (the reader never waits for a
// client), the two that keep up must receive every byte, and the stalled one no more than its window.
func BenchmarkFanOut(b *testing.B) {
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
		b.StopTimer()
		id := "fanout" + strconv.Itoa(i)
		// The program waits for a line before it prints, so every client is attached first.
		script := throughputScript(total)
		argv := []string{"sh", "-c", "read x; " + script[2]}
		term, err := reg.Create(Spec{ID: id, Path: sh, Argv: argv, Cwd: "/", Env: env, Cols: 120, Rows: 40, RingBytes: 8 << 20})
		if err != nil {
			b.Fatal(err)
		}
		done := make(chan struct{})
		sinks := []*countingSink{newCountingSink(false), newCountingSink(false), newCountingSink(true)}
		clients := make([]*Client, len(sinks))
		for n, s := range sinks {
			c, err := term.Attach(context.Background(), ClientOptions{Label: "bench" + strconv.Itoa(n)}, s)
			if err != nil {
				b.Fatal(err)
			}
			frame, err := wire.EncodeAttach(wire.Attach{})
			if err != nil {
				b.Fatal(err)
			}
			c.Frame(frame)
			clients[n] = c
			go s.acknowledge(c, done)
		}
		time.Sleep(50 * time.Millisecond)
		b.StartTimer()
		term.HumanInput([]byte("\r"))
		<-term.Done()
		b.StopTimer()
		head := term.OutputHead()
		if head < total {
			b.Fatalf("only %d bytes arrived", head)
		}
		// The ones that keep up get everything, soon after the program ends.
		for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
			if sinks[0].received.Load() >= head && sinks[1].received.Load() >= head {
				break
			}
		}
		for n, s := range sinks[:2] {
			if got := s.received.Load(); got < head {
				b.Fatalf("client %d that kept up received %d of %d bytes", n, got, head)
			}
		}
		if got := sinks[2].received.Load(); got > int64(config.DefaultWindowBytes) {
			b.Fatalf("the stalled client was sent %d bytes, past its window", got)
		}
		close(done)
		for _, c := range clients {
			c.Detach()
		}
		_ = reg.Forget(id)
		b.StartTimer()
	}
}
