package view

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// eventSink keeps the EVENTs a client sends, decoded.
type eventSink struct {
	mu  sync.Mutex
	got []map[string]any
}

func (s *eventSink) SendChannel(_ uint32, b []byte) error {
	f, err := wire.Decode(b)
	if err != nil || f.Type != wire.TypeEvent {
		return err
	}
	var m map[string]any
	if err := json.Unmarshal(f.JSON, &m); err != nil {
		return err
	}
	s.mu.Lock()
	s.got = append(s.got, m)
	s.mu.Unlock()
	return nil
}

func (s *eventSink) CloseChannel(uint32) {}

func copyClient(sel func(context.Context, *browser.Tab, int) (string, bool, bool, error)) (*Client, *eventSink) {
	s := &eventSink{}
	return &Client{id: "c1", hub: &Hub{Selection: sel}, conn: s, tier: "live", tab: testTab(), quit: make(chan struct{})}, s
}

func TestACopyAnswersTheClientWithTheSelection(t *testing.T) {
	var asked int
	cl, s := copyClient(func(_ context.Context, _ *browser.Tab, max int) (string, bool, bool, error) {
		asked = max
		return "someone@example.com", false, false, nil
	})
	cl.copySelection(context.Background(), cl.tab, "k1")
	if asked != MaxCopyText {
		t.Fatalf("the page was asked for %d characters", asked)
	}
	if len(s.got) != 1 || s.got[0]["type"] != "copied" || s.got[0]["id"] != "k1" || s.got[0]["text"] != "someone@example.com" ||
		s.got[0]["truncated"] != false || s.got[0]["withheld"] != false {
		t.Fatalf("answer: %v", s.got)
	}
}

func TestACopyThatFailsSaysSoAndOneWithoutAnIdIsRefused(t *testing.T) {
	cl, s := copyClient(func(context.Context, *browser.Tab, int) (string, bool, bool, error) {
		return "", false, false, errors.New("a dialog is open")
	})
	cl.copySelection(context.Background(), cl.tab, "k2")
	cl.copySelection(context.Background(), cl.tab, "")
	cl.copySelection(context.Background(), cl.tab, strings.Repeat("x", 65))
	if len(s.got) != 3 || s.got[0]["type"] != "copied" || s.got[0]["error"] != "a dialog is open" || s.got[0]["text"] != "" {
		t.Fatalf("a failed copy: %v", s.got)
	}
	for _, e := range s.got[1:] {
		if e["type"] != "error" || e["code"] != "input" {
			t.Fatalf("a copy without a usable id: %v", e)
		}
	}
}

func TestACopiedTextIsTrimmedToOneFrame(t *testing.T) {
	// A control character is six bytes of JSON: this text is within MaxCopyText and far over a frame.
	text := strings.Repeat("\x01", MaxCopyText-1) + "ж"
	b := copiedEvent("k3", text, false, false)
	if len(b) > protowire.MaxPayload {
		t.Fatalf("an event of %d bytes", len(b))
	}
	f, err := wire.Decode(b)
	if err != nil {
		t.Fatal(err)
	}
	var m struct {
		Text      string `json:"text"`
		Truncated bool   `json:"truncated"`
	}
	if err := json.Unmarshal(f.JSON, &m); err != nil {
		t.Fatal(err)
	}
	if !m.Truncated || m.Text == "" || !utf8.ValidString(m.Text) || !strings.HasPrefix(text, m.Text) {
		t.Fatalf("trimmed to %d bytes, truncated %v", len(m.Text), m.Truncated)
	}
	// A text that fits is left whole.
	if b := copiedEvent("k4", "ok", false, false); !strings.Contains(string(b), `"text":"ok","truncated":false`) {
		t.Fatalf("a short copy: %s", b)
	}
}
