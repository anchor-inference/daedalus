package rpc_test

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/desk"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The chords every system's table refuses under any lease; the fake desktop refuses them all.
var endingChords = []string{
	"alt+F4", "ctrl+alt+Delete", "ctrl+alt+del", "ctrl+alt+BackSpace", "cmd+q", "cmd+option+esc", "win+l",
	"super+l", "cmd+ctrl+q", "cmd+shift+q", "ctrl+shift+esc", "ctrl+alt+l", "ctrl+alt+end",
	"ctrl+alt+f1", "ctrl+alt+f2", "ctrl+alt+f3", "ctrl+alt+f4", "ctrl+alt+f5", "ctrl+alt+f6",
	"ctrl+alt+f7", "ctrl+alt+f8", "ctrl+alt+f9", "ctrl+alt+f10", "ctrl+alt+f11", "ctrl+alt+f12",
}

// The chords that only take the keyboard away from the window in front.
var leavingChords = []string{"alt+tab", "super+tab", "cmd+tab", "super", "win+r", "super+d", "cmd+space", "alt+f2"}

func refusalData(t *testing.T, err error) (int, map[string]any) {
	t.Helper()
	var we *wire.Error
	if !errors.As(err, &we) {
		t.Fatalf("not a refusal: %v", err)
	}
	m, _ := we.Data.(map[string]any)
	return we.Code, m
}

func TestEveryChordThatEndsTheSessionIsRefusedAndNothingIsPressed(t *testing.T) {
	h := start(t, desk.Config{ActionsPerSecond: 1000, ActionBurst: 1000})
	h.grant(map[string]any{"kind": "desktop"})
	for _, chord := range endingChords {
		_, err := h.act(map[string]any{"action": "key", "keys": chord})
		c, data := refusalData(t, err)
		if c != desk.CodeBlockedKey || data["reason"] != "ends" || data["why"] == "" || data["delivery"] != desk.NotDelivered {
			t.Errorf("%q: %v %v", chord, err, data)
		}
	}
	// A refused chord late in a sequence stops the whole sequence before its first key.
	_, err := h.act(map[string]any{"action": "key", "keys": "ctrl+a alt+f4"})
	if c, _ := refusalData(t, err); c != desk.CodeBlockedKey || !strings.Contains(err.Error(), "never presses") {
		t.Fatalf("a sequence ending in alt+f4: %v", err)
	}
	if in := h.fake.Inputs(); len(in) != 0 {
		t.Fatalf("a refused chord reached the desktop: %+v", in)
	}
	// Ordinary chords still go.
	if _, err := h.act(map[string]any{"action": "key", "keys": "ctrl+s ctrl+f4 ctrl+q"}); err != nil {
		t.Fatal(err)
	}
}

func TestAChordThatLeavesTheWindowIsRefusedOnlyUnderANarrowerLease(t *testing.T) {
	h := start(t, desk.Config{ActionsPerSecond: 1000, ActionBurst: 1000})
	h.grant(map[string]any{"kind": "window", "window": "10"})
	for _, chord := range leavingChords {
		_, err := h.act(map[string]any{"action": "key", "keys": chord})
		if c, data := refusalData(t, err); c != desk.CodeBlockedKey || data["reason"] != "leaves" {
			t.Errorf("%q under a window lease: %v %v", chord, err, data)
		}
	}
	if len(h.fake.Inputs()) != 0 {
		t.Fatal("a refused chord reached the desktop")
	}
	h.grant(map[string]any{"kind": "desktop"})
	for _, chord := range leavingChords {
		if _, err := h.act(map[string]any{"action": "key", "keys": chord}); err != nil {
			t.Errorf("%q under a desktop lease: %v", chord, err)
		}
	}
}

func TestActResultsSayWhetherTheirInputWasSent(t *testing.T) {
	h := start(t, desk.Config{ActionsPerSecond: 1, ActionBurst: 1})
	h.subscribe(h.op)
	h.grant(map[string]any{"kind": "window", "window": "10"})
	res, err := h.act(map[string]any{"action": "key", "keys": "ctrl+s"})
	if err != nil || res.Delivery != desk.Delivered {
		t.Fatalf("a key that went: %+v %v", res, err)
	}
	// Past the rate: refused before anything was sent, which is what makes a retry safe.
	_, err = h.act(map[string]any{"action": "key", "keys": "ctrl+s"})
	if c, data := refusalData(t, err); c != desk.CodeRate || data["delivery"] != desk.NotDelivered {
		t.Fatalf("past the rate: %v %v", err, data)
	}
	// Parameters that do not decode are refused before the daemon looks at them, and say so too.
	_, err = h.act(map[string]any{"action": "key", "keys": "enter", "colour": "red"})
	if c, data := refusalData(t, err); c != wire.CodeInvalidParams || data["delivery"] != desk.NotDelivered {
		t.Fatalf("an unknown field: %v %v", err, data)
	}
	// A drag stopped half way has pressed the button: what landed is unknown.
	time.Sleep(1100 * time.Millisecond)
	h.fake.InputDelay = 30 * time.Millisecond
	s := h.shot(map[string]any{})
	done := make(chan error, 1)
	go func() {
		_, err := h.act(map[string]any{"action": "drag", "screenshot_id": s.ID,
			"path": []map[string]int{{"x": 10, "y": 10}, {"x": 700, "y": 500}}})
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	if err := h.op.Call(h.ctx, "stop", nil, nil); err != nil {
		t.Fatal(err)
	}
	select {
	case err = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the drag did not stop")
	}
	if c, data := refusalData(t, err); c != desk.CodeNoLease || data["delivery"] != desk.DeliveryUnknown {
		t.Fatalf("the stopped drag: %v %v", err, data)
	}
}
