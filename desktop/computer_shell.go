package main

// What the desktop shell needs of computer use: whether the agent holds the screen right now, so it
// can draw the border and the "Daedalus is controlling" note over the desktop, arm its Stop shortcut
// and offer Stop in the tray; and the Stop itself.
//
// Both go over the shell's own stdio link rather than the launcher's HTTP API. The routes there ask
// for the token the launcher hands to its page and to the host; the shell never had it, and giving it
// one more holder for a read and a Stop would widen who can grant control for nothing. The launcher
// already holds the operator's connection to the daemon, so it asks, and tells the shell only when
// the answer changes.

import (
	"context"
	"encoding/json"
	"time"
)

// computerWatchEvery is how often the launcher asks the daemon whether a lease is held while the
// shell is attached. A second is the longest the operator should look at an unmarked screen the
// agent is driving; one lease.status over a local socket costs nothing next to that.
const computerWatchEvery = time.Second

// shellComputer is the lease as the shell needs it: held or not, paused by the operator's own hand,
// and the kind of scope (window, app, display, desktop). The time left is left out on purpose: it
// changes every second, and an event per second would be noise for a border that does not show it.
type shellComputer struct {
	Active bool   `json:"active"`
	Paused bool   `json:"paused"`
	Scope  string `json:"scope,omitempty"`
}

// readShellComputer reads lease.status's answer. Anything it cannot read is "no lease": a border
// drawn on a guess would tell the operator the agent has the screen when it may not.
func readShellComputer(raw json.RawMessage) shellComputer {
	var status struct {
		Lease struct {
			Active bool `json:"active"`
			Lease  *struct {
				Scope struct {
					Kind string `json:"kind"`
				} `json:"scope"`
			} `json:"lease"`
		} `json:"lease"`
		Human struct {
			Paused bool `json:"paused"`
		} `json:"human"`
	}
	if json.Unmarshal(raw, &status) != nil || !status.Lease.Active {
		return shellComputer{}
	}
	out := shellComputer{Active: true, Paused: status.Human.Paused}
	if status.Lease.Lease != nil {
		out.Scope = status.Lease.Lease.Scope.Kind
	}
	return out
}

// computerCaller is the one call the watch needs of the operator client, so a test can stand in.
type computerCaller interface {
	Call(ctx context.Context, method string, params any) (json.RawMessage, error)
}

// computerWatch tells the shell about the lease: once whenever it changes, and once more after
// every Stop the shell asks for, whether or not the Stop changed anything, so a Stop that failed
// leaves the border up rather than a shell that hid it in hope.
type computerWatch struct {
	client computerCaller
	emit   func(shellComputer)
	native func() bool
	stops  chan struct{}
	last   shellComputer
}

func newComputerWatch(client computerCaller, native func() bool, emit func(shellComputer)) *computerWatch {
	return &computerWatch{client: client, emit: emit, native: native, stops: make(chan struct{}, 1)}
}

// Stop asks for the daemon's Stop on the watch's own goroutine; a second press while one is
// pending is the same Stop.
func (w *computerWatch) Stop() {
	select {
	case w.stops <- struct{}{}:
	default:
	}
}

// Run polls until ctx ends. Outside native mode, or with no daemon to reach, the state is "no lease"
// and nothing is said: the shell starts from that state, and a desktop without computer use should
// cost it nothing.
func (w *computerWatch) Run(ctx context.Context) {
	ticker := time.NewTicker(computerWatchEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.stops:
			w.stop(ctx)
			w.check(ctx, true)
		case <-ticker.C:
			w.check(ctx, false)
		}
	}
}

func (w *computerWatch) stop(ctx context.Context) {
	if !w.native() {
		return
	}
	callCtx, cancel := context.WithTimeout(ctx, computerCallTimeout)
	defer cancel()
	_, _ = w.client.Call(callCtx, "stop", nil)
}

func (w *computerWatch) check(ctx context.Context, always bool) {
	now := shellComputer{}
	if w.native() {
		callCtx, cancel := context.WithTimeout(ctx, 2*computerWatchEvery)
		raw, err := w.client.Call(callCtx, "lease.status", nil)
		cancel()
		if err == nil {
			now = readShellComputer(raw)
		}
	}
	if now == w.last && !always {
		return
	}
	w.last = now
	w.emit(now)
}
