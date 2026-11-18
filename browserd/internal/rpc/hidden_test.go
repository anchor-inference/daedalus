//go:build unix

package rpc_test

import (
	"strings"
	"testing"
)

// What stands on screen is read, whatever the markup says of it (testdata/site/hidden.html): a form
// a modal library left aria-hidden behind a notice it closed, a form in a shadow root or a frame,
// cards made of divs, and words the outline cannot take from the markup, which a note then gives.
func TestContentOnScreenIsRead(t *testing.T) {
	h := start(t, nil)
	const question = "Почему хотите сняться с воинского учёта?"
	const answer = "Уезжаю жить в другую страну"
	const missed = "misses most of the words on screen"

	o := h.open("g1", "project-a", "/site/hidden.html?mode=aria")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	// While the notice is open its backdrop takes every hit: the form behind it is hidden, as the
	// page says, and the outline is no less than what the screen shows.
	if strings.Contains(s.Text, question) || strings.Contains(s.Text, "aria-hidden") || strings.Contains(s.Text, missed) {
		t.Fatalf("the form behind an open notice:\n%s", s.Text)
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Понятно, перейти к услуге"), "element": "the notice's button"})
	s = h.snapshot(tab)
	if !strings.Contains(s.Text, "[aria-hidden by the page, but on screen and clickable:]") || !strings.Contains(s.Text, `heading "`+question+`"`) ||
		strings.Contains(s.Text, missed) {
		t.Fatalf("the form left aria-hidden after the notice closed:\n%s", s.Text)
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "generic", answer), "element": "the first answer"})
	h.waitTitle("g1", "chose "+answer)
	// A search and the readable text find it too: they are what an agent tries next.
	var found struct {
		Total int `json:"total"`
	}
	h.must("page.find", map[string]any{"tab_id": tab, "query": "другую страну"}, &found)
	if found.Total != 1 {
		t.Fatalf("a search for an answer on screen: %+v", found)
	}

	// Left inert instead, the form is read and said to take no click: a person could not click it
	// either, and the agent must not try it again and again.
	tab = h.open("g-inert", "project-a", "/site/hidden.html?mode=inert").Tab.ID
	s = h.snapshot(tab)
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Понятно, перейти к услуге"), "element": "the notice's button"})
	s = h.snapshot(tab)
	if !strings.Contains(s.Text, "[inert by the page:") || !strings.Contains(s.Text, `generic "`+answer+`"`) {
		t.Fatalf("the form left inert:\n%s", s.Text)
	}

	for _, mode := range []string{"shadow", "frame", "plain"} {
		group := "g-" + mode
		tab := h.open(group, "project-a", "/site/hidden.html?mode="+mode).Tab.ID
		s := h.snapshot(tab)
		if !strings.Contains(s.Text, `heading "`+question+`"`) || strings.Contains(s.Text, missed) || strings.Contains(s.Text, "aria-hidden") {
			t.Fatalf("%s:\n%s", mode, s.Text)
		}
		h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "generic", answer), "element": "the first answer"})
		h.waitTitle(group, "chose "+answer)
	}

	// Answers each marked aria-hidden, too short for the outline to overrule the mark: the note on
	// unread words lists them with refs that click them.
	tab = h.open("g-tiles", "project-a", "/site/hidden.html?mode=tiles").Tab.ID
	s = h.snapshot(tab)
	if !strings.Contains(s.Text, missed) || strings.Contains(s.Text, "the page looks empty") {
		t.Fatalf("tiles:\n%s", s.Text)
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "text", "Санкт-Петербург"), "element": "a city"})
	h.waitTitle("g-tiles", "chose Санкт-Петербург")
}
