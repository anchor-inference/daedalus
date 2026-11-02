package desk

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/audit"
	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// newDesk is the rules over the fake desktop with no watch running: the test calls Tick itself, so
// what ends a lease is the call or the tick the test makes, never a race with a goroutine.
func newDesk(t *testing.T) (*Desk, *fake.Fake, *events.Log) {
	t.Helper()
	al, err := audit.Open(filepath.Join(t.TempDir(), "audit.jsonl"), 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { al.Close() })
	fk := fake.New(fake.DefaultScene())
	evlog := events.NewLog(256)
	d := New(Config{Protected: []Matcher{{BundleID: "com.example.daedalus"}}}, fk, evlog, al, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return d, fk, evlog
}

func wireErr(t *testing.T, err error) *wire.Error {
	t.Helper()
	var we *wire.Error
	if !errors.As(err, &we) {
		t.Fatalf("not a refusal: %v", err)
	}
	return we
}

func dataOf(t *testing.T, err error) map[string]any {
	t.Helper()
	m, ok := wireErr(t, err).Data.(map[string]any)
	if !ok {
		t.Fatalf("data %#v", wireErr(t, err).Data)
	}
	return m
}

func lockedScreen() fake.Simulation {
	return fake.Simulation{Secure: &backend.Secure{Locked: true, Reason: "lock_screen"}}
}

func published(evlog *events.Log) []events.Event {
	evs, _ := evlog.After(0, 1000)
	return evs
}

func ended(t *testing.T, evlog *events.Log) map[string]any {
	t.Helper()
	for _, e := range published(evlog) {
		if e.Type == "lease.ended" {
			return e.Data.(map[string]any)
		}
	}
	t.Fatal("no lease.ended event")
	return nil
}

func TestACallOnALockedScreenEndsTheLeaseForGood(t *testing.T) {
	d, fk, evlog := newDesk(t)
	ctx := context.Background()
	if _, err := d.Grant(ctx, GrantParams{Session: "s", Scope: Scope{Kind: ScopeDesktop}}); err != nil {
		t.Fatal(err)
	}
	_ = fk.Simulate(lockedScreen())
	_, err := d.Act(ctx, ActParams{Session: "s", Action: ActKey, Keys: "enter"})
	if we := wireErr(t, err); we.Code != CodeSecure || dataOf(t, err)["lease_ended"] != true {
		t.Fatalf("a key on a locked screen: %v %#v", err, we.Data)
	}
	if st := d.LeaseStatus(); st.Active || st.LastEnded != EndedLocked {
		t.Fatalf("status after the lock: %+v", st)
	}
	if e := ended(t, evlog); e["reason"] != EndedLocked || e["by"] != "daemon" || e["secure"] != "lock_screen" {
		t.Fatalf("lease.ended %#v", e)
	}
	// Unlocking does not hand the desktop back: the operator must grant it again.
	_ = fk.Simulate(fake.Simulation{Secure: &backend.Secure{}})
	_, err = d.Act(ctx, ActParams{Session: "s", Action: ActWait, Ms: 1})
	if we := wireErr(t, err); we.Code != CodeNoLease || dataOf(t, err)["reason"] != EndedLocked {
		t.Fatalf("after unlocking: %v %#v", err, we.Data)
	}
	if len(fk.Inputs()) != 0 {
		t.Fatal("input reached a locked screen")
	}
}

func TestALockBetweenCallsEndsTheLeaseAndCutsTheActionShort(t *testing.T) {
	d, fk, evlog := newDesk(t)
	ctx := context.Background()
	if _, err := d.Grant(ctx, GrantParams{Session: "s", Scope: Scope{Kind: ScopeWindow, Window: "10"}}); err != nil {
		t.Fatal(err)
	}
	fk.InputDelay = 20 * time.Millisecond
	done := make(chan error, 1)
	go func() {
		// Long enough to be in flight when the screen locks: 400 characters in chunks of 40.
		_, err := d.Act(ctx, ActParams{Session: "s", Action: ActType, Text: strings.Repeat("a", 400)})
		done <- err
	}()
	time.Sleep(60 * time.Millisecond)
	_ = fk.Simulate(fake.Simulation{Secure: &backend.Secure{Locked: true, Reason: "secure_desktop"}})
	if d.Tick(ctx) {
		t.Fatal("the tick left the lease active on a secure desktop")
	}
	var err error
	select {
	case err = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the action went on behind the lock")
	}
	// Part of the text went out before the lock: the host must not repeat it blindly.
	if we := wireErr(t, err); we.Code != CodeNoLease || dataOf(t, err)["delivery"] != DeliveryUnknown {
		t.Fatalf("the cut action: %v %#v", err, we.Data)
	}
	if e := ended(t, evlog); e["reason"] != EndedLocked || e["secure"] != "secure_desktop" {
		t.Fatalf("lease.ended %#v", e)
	}
	stopped := false
	for _, e := range published(evlog) {
		stopped = stopped || e.Type == "stopped" && e.Data.(map[string]any)["reason"] == EndedLocked
	}
	if !stopped {
		t.Fatal("no stopped event for the lock")
	}
}

func TestEveryActResultSaysWhetherItsInputWasSent(t *testing.T) {
	d, fk, _ := newDesk(t)
	ctx := context.Background()
	if _, err := d.Grant(ctx, GrantParams{Session: "s", Scope: Scope{Kind: ScopeWindow, Window: "10"}}); err != nil {
		t.Fatal(err)
	}
	res, err := d.Act(ctx, ActParams{Session: "s", Action: ActKey, Keys: "ctrl+s"})
	if err != nil || res.Delivery != Delivered {
		t.Fatalf("a key that went: %+v %v", res, err)
	}
	// Refused before the first event: nothing was sent, so a retry is safe.
	_ = fk.Simulate(fake.Simulation{Focus: "12"})
	_, err = d.Act(ctx, ActParams{Session: "s", Action: ActKey, Keys: "enter"})
	if wireErr(t, err).Code != CodeOutOfScope || dataOf(t, err)["delivery"] != NotDelivered {
		t.Fatalf("out of scope: %v", err)
	}
	_, err = d.Act(ctx, ActParams{Session: "s", Action: "fly"})
	if wireErr(t, err).Code != wire.CodeInvalidParams || dataOf(t, err)["delivery"] != NotDelivered {
		t.Fatalf("a bad action: %v", err)
	}
	// The pause's data is a struct; the delivery joins its fields rather than replacing them.
	d.mu.Lock()
	d.paused, d.pausedAt, d.lastHuman = true, d.now(), d.now()
	d.mu.Unlock()
	_, err = d.Act(ctx, ActParams{Session: "s", Action: ActWait, Ms: 1})
	if m := dataOf(t, err); wireErr(t, err).Code != CodeHumanDriving || m["delivery"] != NotDelivered || m["paused"] != true {
		t.Fatalf("paused: %v %#v", err, m)
	}
	if Undelivered(errors.New("json: unknown field")).(*wire.Error).Data.(map[string]any)["delivery"] != NotDelivered {
		t.Fatal("a parameter error is not marked")
	}
}

func TestAWaitStoppedHalfWaySentNothing(t *testing.T) {
	d, _, _ := newDesk(t)
	ctx := context.Background()
	if _, err := d.Grant(ctx, GrantParams{Session: "s", Scope: Scope{Kind: ScopeDesktop}}); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		_, err := d.Act(ctx, ActParams{Session: "s", Action: ActWait, Ms: 3000})
		done <- err
	}()
	time.Sleep(50 * time.Millisecond)
	d.Stop("operator")
	err := <-done
	if wireErr(t, err).Code != CodeNoLease || dataOf(t, err)["delivery"] != NotDelivered {
		t.Fatalf("the stopped wait: %v", err)
	}
}
