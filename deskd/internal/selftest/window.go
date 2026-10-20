package selftest

import (
	"context"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

// The colours of the test window's four quadrants: top left, top right, bottom left, bottom right.
// They are pure so that a downscaled screenshot still shows each one unmistakably at its centre.
var quadrantColors = [4][3]uint8{{255, 0, 0}, {0, 200, 0}, {0, 0, 255}, {255, 255, 0}}

// Event is what the test window received, in the window's own terms.
type Event struct {
	// Kind is press, release, motion, wheel, char or key.
	Kind string
	// X and Y are where a press, release or motion happened, in desktop units from the top-left
	// corner of the window's content.
	X, Y float64
	// Button is 1 left, 2 middle, 3 right.
	Button int
	// Wheel is notches of a wheel event, positive down.
	Wheel int
	// Text is the character a char event delivered.
	Text string
	// Key is a chord a key event delivered in canonical form ("ctrl+a", "enter"): reported for named
	// keys and for chords with Ctrl, Alt or Super, where no character arrives.
	Key string
}

// Window is a small window the selftest opens and drives, and nothing else: it paints the four
// quadrants over its whole content and records every event it receives.
type Window interface {
	// ID is the window's id in the backend's terms (backend.Window.ID).
	ID() string
	// Content is the window's content area in desktop units.
	Content() (geom.Rect, error)
	// Events returns what arrived since the last call.
	Events() []Event
	// MoveBy moves the window.
	MoveBy(dx, dy float64) error
	Close() error
}

// opener opens the test window of this system; each system's file sets it.
var opener func(ctx context.Context, title string, w, h int) (Window, error)
