package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"testing"
)

type fakeComputerCaller struct {
	status string
	// answers are what methods other than lease.status and stop return.
	answers map[string]string
	// failing lists methods that fail while the rest answer.
	failing map[string]bool
	err     error
	calls   []string
}

func (f *fakeComputerCaller) Call(_ context.Context, method string, _ any) (json.RawMessage, error) {
	f.calls = append(f.calls, method)
	if f.err != nil {
		return nil, f.err
	}
	if f.failing[method] {
		return nil, errors.New(method + " failed")
	}
	if method == "stop" {
		return json.RawMessage(`{"stopped":true}`), nil
	}
	if answer, ok := f.answers[method]; ok {
		return json.RawMessage(answer), nil
	}
	return json.RawMessage(f.status), nil
}

// The shell draws its border from this and nothing else, so what cannot be read is no lease.
func TestTheShellReadsTheLeaseOrNothing(t *testing.T) {
	cases := []struct {
		raw  string
		want shellComputer
	}{
		{`{"lease":{"active":true,"lease":{"id":"l1","session":"s1","scope":{"kind":"window","window":"10"}},"remaining_ms":5000},"human":{"paused":false}}`, shellComputer{Active: true, Scope: "window"}},
		{`{"lease":{"active":true,"lease":{"scope":{"kind":"desktop"}}},"human":{"paused":true}}`, shellComputer{Active: true, Paused: true, Scope: "desktop"}},
		{`{"lease":{"active":false,"last_ended":"stopped"},"human":{"paused":true}}`, shellComputer{}},
		{`not json`, shellComputer{}},
	}
	for _, c := range cases {
		raw, want := c.raw, c.want
		if got, _ := readShellComputer(json.RawMessage(raw)); !got.same(want) {
			t.Errorf("%s: %+v, want %+v", raw, got, want)
		}
	}
}

// One event per change, none while nothing changes (the time left ticking is not a change), and one
// after every Stop even when the Stop changed nothing, so a failed Stop leaves the border standing.
func TestTheShellHearsOfAChangeOnceAndAfterEveryStop(t *testing.T) {
	held := `{"lease":{"active":true,"lease":{"scope":{"kind":"app"}},"remaining_ms":%d},"human":{"paused":false}}`
	fake := &fakeComputerCaller{status: `{"lease":{"active":false},"human":{"paused":false}}`, answers: map[string]string{"apps.list": `{"apps":[]}`}}
	var told []shellComputer
	watch := newComputerWatch(fake, func() bool { return true }, func(state shellComputer) { told = append(told, state) })
	ctx := context.Background()
	watch.check(ctx, false)
	if len(told) != 0 {
		t.Fatalf("the shell starts from no lease and was told of it anyway: %+v", told)
	}
	fake.status = fmt.Sprintf(held, 60000)
	watch.check(ctx, false)
	fake.status = fmt.Sprintf(held, 59000)
	watch.check(ctx, false)
	if len(told) != 1 || !told[0].same(shellComputer{Active: true, Scope: "app"}) {
		t.Fatalf("told %+v", told)
	}
	watch.stop(ctx)
	watch.check(ctx, true)
	if len(told) != 2 || !told[1].same(told[0]) {
		t.Fatalf("a Stop that changed nothing must still be answered: %+v", told)
	}
	if !slices.Equal(fake.calls[len(fake.calls)-3:], []string{"stop", "lease.status", "apps.list"}) {
		t.Fatalf("calls %v", fake.calls)
	}
	fake.err = errors.New("the daemon went away")
	watch.check(ctx, false)
	if len(told) != 3 || told[2].Active {
		t.Fatalf("a daemon that cannot be reached holds no lease: %+v", told)
	}
}

// Docker mode has no desktop to drive: the daemon is never asked, and the shell is never told.
func TestOutsideNativeModeTheWatchIsSilent(t *testing.T) {
	fake := &fakeComputerCaller{status: `{"lease":{"active":true},"human":{}}`}
	var told []shellComputer
	watch := newComputerWatch(fake, func() bool { return false }, func(state shellComputer) { told = append(told, state) })
	watch.check(context.Background(), false)
	watch.stop(context.Background())
	if len(fake.calls) != 0 || len(told) != 0 {
		t.Fatalf("calls %v, told %+v", fake.calls, told)
	}
}

// The border goes around what the lease covers and nothing else: one display, every display, one
// window, or every window of one app on screen.
func TestTheTargetsAreWhatTheLeaseCovers(t *testing.T) {
	info := `{"displays":[{"id":"\\\\.\\DISPLAY1","bounds":{"x":0,"y":0,"w":2560,"h":1440}},{"id":"\\\\.\\DISPLAY2","bounds":{"x":-1920,"y":200,"w":1920,"h":1080}}]}`
	apps := `{"apps":[
		{"id":"pid:1","windows":[{"id":"0x10","bounds":{"x":100,"y":80,"w":800,"h":600}},{"id":"0x11","bounds":{"x":-1800,"y":300,"w":400,"h":300}},
			{"id":"0x12","bounds":{"x":-32000,"y":-32000,"w":160,"h":28},"minimized":true}]},
		{"id":"pid:2","windows":[{"id":"0x20","bounds":{"x":900,"y":100,"w":500,"h":500}}]}]}`
	for _, c := range []struct {
		scope leaseScope
		want  []shellTarget
	}{
		{leaseScope{Kind: "display", Display: `\\.\DISPLAY2`}, []shellTarget{{"display", -1920, 200, 1920, 1080}}},
		{leaseScope{Kind: "desktop"}, []shellTarget{{"display", 0, 0, 2560, 1440}, {"display", -1920, 200, 1920, 1080}}},
		{leaseScope{Kind: "window", Window: "0x20"}, []shellTarget{{"window", 900, 100, 500, 500}}},
		{leaseScope{Kind: "app", App: "pid:1"}, []shellTarget{{"window", 100, 80, 800, 600}, {"window", -1800, 300, 400, 300}}},
		{leaseScope{Kind: "window", Window: "0x12"}, nil},
		{leaseScope{Kind: "display", Display: "gone"}, nil},
	} {
		if got := readTargets(c.scope, json.RawMessage(info), json.RawMessage(apps)); !slices.Equal(got, c.want) {
			t.Errorf("%+v: %+v, want %+v", c.scope, got, c.want)
		}
	}
}

// A window that moves is a change the shell hears of, so the border follows it; a window list that
// fails once keeps the border where it was rather than taking it down.
func TestTheBorderFollowsAMovingWindow(t *testing.T) {
	fake := &fakeComputerCaller{
		status:  `{"lease":{"active":true,"lease":{"scope":{"kind":"window","window":"0x10"}}},"human":{"paused":false}}`,
		answers: map[string]string{"apps.list": `{"apps":[{"id":"pid:1","windows":[{"id":"0x10","bounds":{"x":10,"y":10,"w":300,"h":200}}]}]}`},
	}
	var told []shellComputer
	watch := newComputerWatch(fake, func() bool { return true }, func(state shellComputer) { told = append(told, state) })
	ctx := context.Background()
	watch.check(ctx, false)
	fake.answers["apps.list"] = `{"apps":[{"id":"pid:1","windows":[{"id":"0x10","bounds":{"x":50,"y":10,"w":300,"h":200}}]}]}`
	watch.check(ctx, false)
	watch.check(ctx, false)
	if len(told) != 2 || told[1].Targets[0].X != 50 || told[0].Targets[0].X != 10 {
		t.Fatalf("told %+v", told)
	}
	fake.failing = map[string]bool{"apps.list": true}
	watch.check(ctx, false)
	if len(told) != 2 {
		t.Fatalf("a failed window list changed the border: %+v", told)
	}
}
