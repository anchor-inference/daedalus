package app

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/guard"
	"github.com/ascorblack/daedalus/cli/internal/tui"
)

func TestLocalGuardAsksThroughTheScreen(t *testing.T) {
	g := guard.New(guard.Ask, nil)
	b := New(Options{Client: client.New("http://127.0.0.1:1", ""), Guard: g, Interactive: true})
	defer b.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- g.Check(ctx, guard.Action{Kind: guard.Exec, Method: "exec.run", Command: "npm test"})
	}()
	var req tui.PermissionRequest
	for req.ID == "" {
		select {
		case ev := <-b.Events():
			if pr, ok := ev.(tui.PermissionRequested); ok {
				req = pr.Request
			}
		case <-ctx.Done():
			t.Fatal("no permission request reached the screen")
		}
	}
	if req.Source != tui.SourceLocal || req.Text != "npm test" || req.Similar != "npm test *" {
		t.Errorf("request: %+v", req)
	}
	if err := b.Decide(ctx, req.ID, tui.Decision{Kind: tui.Deny, Feedback: "run make check"}); err != nil {
		t.Fatal(err)
	}
	err := <-result
	var r *guard.Refusal
	if !errors.As(err, &r) || r.Reason != "the operator said no: run make check" {
		t.Fatalf("%v", err)
	}
}

func TestTranslateFoldsServerEvents(t *testing.T) {
	b := New(Options{Client: client.New("http://127.0.0.1:1", "")})
	defer b.Close()
	ctx := context.Background()
	feed := func(kind, run string, payload map[string]any) []tui.Event {
		raw, _ := json.Marshal(payload)
		return b.translate(ctx, "s1", client.Envelope{Kind: kind, RunID: run, Payload: raw})
	}
	got := feed("message_start", "r1", map[string]any{})
	if len(got) != 1 {
		t.Fatalf("a new run starts a turn: %#v", got)
	}
	if _, ok := got[0].(tui.TurnStarted); !ok {
		t.Fatalf("%#v", got)
	}
	got = feed("content_block_delta", "r1", map[string]any{"delta": map[string]any{"type": "text_delta", "text": "hi"}})
	if d, ok := got[0].(tui.TextDelta); !ok || d.Text != "hi" {
		t.Fatalf("%#v", got)
	}
	feed("tool_use_start", "r1", map[string]any{"tool_call_id": "c1", "tool_name": "Exec"})
	got = feed("tool_use_stop", "r1", map[string]any{"tool_call_id": "c1", "final_input": map[string]any{"command": "ls"}})
	if s, ok := got[0].(tui.ToolStarted); !ok || s.Name != "Exec" || s.Input["command"] != "ls" {
		t.Fatalf("%#v", got)
	}
	got = feed("tool_result", "r1", map[string]any{"tool_call_id": "c1", "content": "a\nb", "is_error": false})
	if f, ok := got[0].(tui.ToolFinished); !ok || f.Output != "a\nb" {
		t.Fatalf("%#v", got)
	}
	got = feed("run_settled", "r1", map[string]any{"status": "stopped"})
	if e, ok := got[0].(tui.TurnEnded); !ok || e.Outcome != tui.TurnInterrupted {
		t.Fatalf("%#v", got)
	}
	if got := feed("some_future_kind", "r1", map[string]any{}); len(got) != 0 {
		t.Errorf("an unknown kind produced %#v", got)
	}
}

func TestHistoryFromTranscript(t *testing.T) {
	var msgs []client.Message
	_ = json.Unmarshal([]byte(`[
	 {"seq": 1, "role": "user", "text": "fix it", "origin": "app"},
	 {"seq": 2, "role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "Exec", "arguments": {"command": "ls"}}]},
	 {"seq": 3, "role": "tool", "tool_results": [{"id": "c1", "content": "a", "is_error": false}]},
	 {"seq": 4, "role": "assistant", "text": "done"},
	 {"seq": 5, "role": "user", "text": "hidden", "internal": true}]`), &msgs)
	h := History(msgs)
	if len(h) != 5 {
		t.Fatalf("%d events: %#v", len(h), h)
	}
	if u, ok := h[0].(tui.UserMessage); !ok || u.Origin != "app" {
		t.Errorf("%#v", h[0])
	}
	if _, ok := h[1].(tui.ToolStarted); !ok {
		t.Errorf("%#v", h[1])
	}
	if _, ok := h[2].(tui.ToolFinished); !ok {
		t.Errorf("%#v", h[2])
	}
}

func TestAParkedTurnIsNotAnEndedOne(t *testing.T) {
	b := New(Options{Client: client.New("http://127.0.0.1:1", "")})
	defer b.Close()
	ctx := context.Background()
	feed := func(kind, run string, payload map[string]any) []tui.Event {
		raw, _ := json.Marshal(payload)
		return b.translate(ctx, "s1", client.Envelope{Kind: kind, RunID: run, Payload: raw})
	}
	feed("message_start", "r1", map[string]any{})
	got := feed("tool_call_pending", "r1", map[string]any{"kind": "external_call", "external_call_payload": map[string]any{"device_name": "laptop"}})
	if w, ok := got[0].(tui.DeviceWaiting); !ok || !w.Waiting || w.Device != "laptop" {
		t.Fatalf("%#v", got)
	}
	if got := feed("run_settled", "r1", map[string]any{"status": "awaiting"}); len(got) != 0 {
		t.Fatalf("a parked turn ended: %#v", got)
	}
	got = feed("message_start", "r2", map[string]any{})
	var resumed, started bool
	for _, e := range got {
		if w, ok := e.(tui.DeviceWaiting); ok && !w.Waiting {
			resumed = true
		}
		if _, ok := e.(tui.TurnStarted); ok {
			started = true
		}
	}
	if !resumed || !started {
		t.Fatalf("the resumed turn: %#v", got)
	}
	if got := feed("run_settled", "r2", map[string]any{"status": "completed"}); len(got) != 1 {
		t.Fatalf("the resumed turn's end: %#v", got)
	}
}
