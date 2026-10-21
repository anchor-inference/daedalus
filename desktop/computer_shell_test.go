package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"
)

type fakeComputerCaller struct {
	status string
	err    error
	calls  []string
}

func (f *fakeComputerCaller) Call(_ context.Context, method string, _ any) (json.RawMessage, error) {
	f.calls = append(f.calls, method)
	if f.err != nil {
		return nil, f.err
	}
	if method == "stop" {
		return json.RawMessage(`{"stopped":true}`), nil
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
		if got := readShellComputer(json.RawMessage(raw)); got != want {
			t.Errorf("%s: %+v, want %+v", raw, got, want)
		}
	}
}

// One event per change, none while nothing changes (the time left ticking is not a change), and one
// after every Stop even when the Stop changed nothing, so a failed Stop leaves the border standing.
func TestTheShellHearsOfAChangeOnceAndAfterEveryStop(t *testing.T) {
	held := `{"lease":{"active":true,"lease":{"scope":{"kind":"app"}},"remaining_ms":%d},"human":{"paused":false}}`
	fake := &fakeComputerCaller{status: `{"lease":{"active":false},"human":{"paused":false}}`}
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
	if len(told) != 1 || told[0] != (shellComputer{Active: true, Scope: "app"}) {
		t.Fatalf("told %+v", told)
	}
	watch.stop(ctx)
	watch.check(ctx, true)
	if len(told) != 2 || told[1] != told[0] {
		t.Fatalf("a Stop that changed nothing must still be answered: %+v", told)
	}
	if fake.calls[len(fake.calls)-2] != "stop" {
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
