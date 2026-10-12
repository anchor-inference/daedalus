//go:build unix

package rpc_test

import (
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
)

type seen struct {
	Moves   int `json:"moves"`
	Wheels  int `json:"wheels"`
	Keys    int `json:"keys"`
	Shifts  int `json:"shifts"`
	Clicked int `json:"clicked"`
	Jumps   int `json:"jumps"`
}

// An agent's action is made as a hand makes it: the pointer travels to the element over several
// moves, an element below the fold is wheeled into view a notch at a time rather than jumped to,
// and text is typed a key at a time, Shift held for the capitals — and the whole stays within the
// budget the deployment gives it. With input.human off the same actions are the old instant ones.
func TestAgentInputIsAHands(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/hand")
	tab := o.Tab.ID
	s := h.snapshot(tab)

	text := "Hello, Мир 42!"
	began := time.Now()
	h.mustAct(tab, map[string]any{"action": "type", "ref": refOf(t, s, "textbox", "Name"), "text": text, "element": "name"})
	typed := time.Since(began)
	var value string
	h.eval(tab, `document.getElementById("name").value`, &value)
	if value != text {
		t.Fatalf("typed %q, the field holds %q", text, value)
	}
	var got seen
	h.eval(tab, "seen", &got)
	if got.Keys < len([]rune(text)) || got.Shifts < 2 || got.Moves < 3 {
		t.Fatalf("not typed by hand: %+v", got)
	}

	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Far button"), "element": "far"})
	h.eval(tab, "seen", &got)
	if got.Clicked != 1 || got.Wheels < 2 {
		t.Fatalf("the far button was not wheeled to and clicked: %+v", got)
	}
	if got.Jumps > 0 {
		t.Fatalf("the page jumped to the button rather than scrolling: %+v", got)
	}
	// The typing's added time is the budget; the round trips and the settle are the rest. A
	// generous bound, since the gate runs on a loaded machine: what it catches is a plan that
	// ignores its budget, which would take seconds for this text.
	if typed > 3*time.Second {
		t.Fatalf("typing %d characters took %s", len([]rune(text)), typed)
	}
}

func TestPlainInputIsInstant(t *testing.T) {
	h := startFull(t, nil, nil, nil, func(c *config.Config) { c.Input.Plain = true })
	o := h.open("g1", "project-a", "/hand")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	h.mustAct(tab, map[string]any{"action": "type", "ref": refOf(t, s, "textbox", "Name"), "text": "Hello", "element": "name"})
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Far button"), "element": "far"})
	var got seen
	h.eval(tab, "seen", &got)
	// No travel (a headed browser adds a move of its own when the page scrolls under the pointer),
	// no wheel, the page jumped to the button, the text inserted with no key.
	if got.Clicked != 1 || got.Wheels != 0 || got.Keys != 0 || got.Moves > 3 || got.Jumps == 0 {
		t.Fatalf("plain input was not the old instant one: %+v", got)
	}
}
