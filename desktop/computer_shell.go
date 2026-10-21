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
	"slices"
	"time"
)

// computerWatchEvery is how often the launcher asks the daemon whether a lease is held while the
// shell is attached. A second is the longest the operator should look at an unmarked screen the
// agent is driving; one lease.status over a local socket costs nothing next to that.
const computerWatchEvery = time.Second

// computerTrackEvery is how often the target is read again while a lease is held, so the border
// follows a window the operator or the agent moves or resizes. Twice a second keeps up with a
// window dragged at a normal pace; the border trailing a fast drag by half a second is acceptable,
// a window list read sixty times a second is not.
const computerTrackEvery = 500 * time.Millisecond

// shellComputer is the lease as the shell needs it: held or not, paused by the operator's own hand,
// the kind of scope (window, app, display, desktop) and the rectangles the border goes around. The
// time left is left out on purpose: it changes every second, and an event per second would be noise
// for a border that does not show it.
type shellComputer struct {
	Active bool   `json:"active"`
	Paused bool   `json:"paused"`
	Scope  string `json:"scope,omitempty"`
	// Targets are in the daemon's desktop units: physical pixels on Windows and X11, points on
	// macOS. The shell turns them into its own device-independent pixels, because only it knows how
	// Electron lays its windows out on each display.
	Targets []shellTarget `json:"targets,omitempty"`
}

// shellTarget is one rectangle the border goes around. A display's border lies inside it, along the
// display's outermost pixels; a window's lies outside it, so the strips never sit on the window the
// agent is clicking in.
type shellTarget struct {
	Kind string  `json:"kind"` // display or window
	X    float64 `json:"x"`
	Y    float64 `json:"y"`
	W    float64 `json:"w"`
	H    float64 `json:"h"`
}

func (a shellComputer) same(b shellComputer) bool {
	return a.Active == b.Active && a.Paused == b.Paused && a.Scope == b.Scope && slices.Equal(a.Targets, b.Targets)
}

// leaseScope is what the lease covers, by the daemon's ids.
type leaseScope struct {
	Kind    string `json:"kind"`
	Window  string `json:"window,omitempty"`
	App     string `json:"app,omitempty"`
	Display string `json:"display,omitempty"`
}

// readShellComputer reads lease.status's answer. Anything it cannot read is "no lease": a border
// drawn on a guess would tell the operator the agent has the screen when it may not.
func readShellComputer(raw json.RawMessage) (shellComputer, leaseScope) {
	var status struct {
		Lease struct {
			Active bool `json:"active"`
			Lease  *struct {
				Scope leaseScope `json:"scope"`
			} `json:"lease"`
		} `json:"lease"`
		Human struct {
			Paused bool `json:"paused"`
		} `json:"human"`
	}
	if json.Unmarshal(raw, &status) != nil || !status.Lease.Active {
		return shellComputer{}, leaseScope{}
	}
	out := shellComputer{Active: true, Paused: status.Human.Paused}
	var scope leaseScope
	if status.Lease.Lease != nil {
		scope = status.Lease.Lease.Scope
		out.Scope = scope.Kind
	}
	return out, scope
}

type computerRect struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	W float64 `json:"w"`
	H float64 `json:"h"`
}

// readTargets picks the rectangles a scope covers out of the daemon's display list (info) or window
// list (apps.list). A window or an app with nothing on screen has no target; the shell then draws
// no border, the note alone saying the agent holds the screen.
func readTargets(scope leaseScope, info, apps json.RawMessage) []shellTarget {
	var out []shellTarget
	switch scope.Kind {
	case "display", "desktop":
		var answer struct {
			Displays []struct {
				ID     string       `json:"id"`
				Bounds computerRect `json:"bounds"`
			} `json:"displays"`
		}
		if json.Unmarshal(info, &answer) != nil {
			return nil
		}
		for _, d := range answer.Displays {
			if scope.Kind == "desktop" || d.ID == scope.Display {
				out = append(out, shellTarget{Kind: "display", X: d.Bounds.X, Y: d.Bounds.Y, W: d.Bounds.W, H: d.Bounds.H})
			}
		}
	case "window", "app":
		var answer struct {
			Apps []struct {
				ID      string `json:"id"`
				Windows []struct {
					ID        string       `json:"id"`
					Bounds    computerRect `json:"bounds"`
					Minimized bool         `json:"minimized"`
				} `json:"windows"`
			} `json:"apps"`
		}
		if json.Unmarshal(apps, &answer) != nil {
			return nil
		}
		for _, app := range answer.Apps {
			for _, w := range app.Windows {
				mine := (scope.Kind == "window" && w.ID == scope.Window) || (scope.Kind == "app" && app.ID == scope.App)
				if mine && !w.Minimized && w.Bounds.W > 0 && w.Bounds.H > 0 {
					out = append(out, shellTarget{Kind: "window", X: w.Bounds.X, Y: w.Bounds.Y, W: w.Bounds.W, H: w.Bounds.H})
				}
			}
		}
	}
	return out
}

// computerCaller is the one call the watch needs of the operator client, so a test can stand in.
type computerCaller interface {
	Call(ctx context.Context, method string, params any) (json.RawMessage, error)
}

// computerWatch tells the shell about the lease: once whenever it changes (its target moving
// included), and once more after every Stop the shell asks for, whether or not the Stop changed
// anything, so a Stop that failed leaves the border up rather than a shell that hid it in hope.
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

// Run polls until ctx ends: every second while no lease is held, twice a second while one is, to
// follow its target. Outside native mode, or with no daemon to reach, the state is "no lease" and
// nothing is said: the shell starts from that state, and a desktop without computer use should cost
// it nothing.
func (w *computerWatch) Run(ctx context.Context) {
	timer := time.NewTimer(computerWatchEvery)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.stops:
			w.stop(ctx)
			w.check(ctx, true)
		case <-timer.C:
			w.check(ctx, false)
		}
		next := computerWatchEvery
		if w.last.Active {
			next = computerTrackEvery
		}
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(next)
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
		if err == nil {
			var scope leaseScope
			now, scope = readShellComputer(raw)
			if now.Active {
				now.Targets = w.targets(callCtx, scope)
			}
		}
		cancel()
	}
	if now.same(w.last) && !always {
		return
	}
	w.last = now
	w.emit(now)
}

// targets asks the daemon for the one list the scope needs. A failed read keeps the last targets
// rather than none: a border that blinks off for one missed answer would look like the lease ended.
func (w *computerWatch) targets(ctx context.Context, scope leaseScope) []shellTarget {
	method := "info"
	if scope.Kind == "window" || scope.Kind == "app" {
		method = "apps.list"
	}
	raw, err := w.client.Call(ctx, method, nil)
	if err != nil {
		if w.last.Active && w.last.Scope == scope.Kind {
			return w.last.Targets
		}
		return nil
	}
	if method == "info" {
		return readTargets(scope, raw, nil)
	}
	return readTargets(scope, nil, raw)
}
