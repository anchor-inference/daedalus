package device

import (
	"context"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// outputStream sends a running command's output to the server as exec.output notifications, a few
// times a second, so the app and the screen show a long build as it goes. It is best effort: output
// written while the link is down, or faster than the cap below between two sends, is not streamed,
// and the call's result carries all of it anyway.
type outputStream struct {
	link *Link
	id   string

	mu      sync.Mutex
	pending []byte
	dropped bool
	stop    chan struct{}
	done    chan struct{}
}

const (
	outputEvery   = 250 * time.Millisecond
	outputPending = 256 << 10
	outputChunk   = 64 << 10
)

func newOutputStream(l *Link, id string) *outputStream {
	o := &outputStream{link: l, id: id, stop: make(chan struct{}), done: make(chan struct{})}
	go o.run()
	return o
}

// Write never blocks and never fails: the command waits on it.
func (o *outputStream) Write(p []byte) (int, error) {
	o.mu.Lock()
	if room := outputPending - len(o.pending); room > 0 {
		o.pending = append(o.pending, p[:min(room, len(p))]...)
	}
	if len(o.pending) >= outputPending {
		o.dropped = true
	}
	o.mu.Unlock()
	return len(p), nil
}

func (o *outputStream) run() {
	defer close(o.done)
	ticker := time.NewTicker(outputEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			o.flush(false)
		case <-o.stop:
			o.flush(true)
			return
		}
	}
}

// close sends what is left and waits for it, so the last output goes before the result.
func (o *outputStream) close() {
	close(o.stop)
	<-o.done
}

func (o *outputStream) flush(final bool) {
	o.mu.Lock()
	data := o.pending
	// A rune cut by the pipe's buffer waits for its other half, unless nothing more is coming.
	keep := 0
	if !final {
		for i := len(data) - 1; i >= 0 && i >= len(data)-utf8.UTFMax; i-- {
			if utf8.RuneStart(data[i]) {
				if !utf8.FullRune(data[i:]) {
					keep = len(data) - i
				}
				break
			}
		}
	}
	o.pending = append([]byte(nil), data[len(data)-keep:]...)
	data = data[:len(data)-keep]
	dropped := o.dropped
	o.dropped = false
	o.mu.Unlock()
	if len(data) == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for len(data) > 0 {
		n := min(outputChunk, len(data))
		for n < len(data) && n > 0 && !utf8.RuneStart(data[n]) {
			n--
		}
		if n == 0 {
			n = min(outputChunk, len(data))
		}
		text := string(data[:n])
		data = data[n:]
		if dropped && len(data) == 0 {
			text += "\n[… output too fast to stream; the result has all of it …]\n"
		}
		o.link.Notify(ctx, "exec.output", map[string]string{"id": o.id, "data": strings.ToValidUTF8(text, "\uFFFD")})
	}
}
