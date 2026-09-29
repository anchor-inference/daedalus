package page

import (
	"strings"
	"testing"
)

func TestRefsNameTheirDocument(t *testing.T) {
	for s, want := range map[string]bool{"e1": true, "e12": true, "f1e5": true, "f2f1e3": true, "f10f2e7": true,
		"": false, "e": false, "f1": false, "f1f2": false, "fe1": false, "x1": false, "e1x": false, "f1e": false, "ef1": false} {
		if got := isRef(s); got != want {
			t.Errorf("isRef(%q) = %v", s, got)
		}
	}
	for s, want := range map[string]bool{"f1": true, "f2f1": true, "f12": true, "": false, "f": false, "f1e2": false, "e1": false, "ff1": false} {
		if got := isFrameRef(s); got != want {
			t.Errorf("isFrameRef(%q) = %v", s, got)
		}
	}
}

func TestAFramesLinesSitUnderItsOwn(t *testing.T) {
	got := indent([]string{"- button \"Pay\" [ref=f1e1]", "  - text \"x\""}, 2)
	if got[0] != "    - button \"Pay\" [ref=f1e1]" || got[1] != "      - text \"x\"" {
		t.Fatalf("%q", got)
	}
}

func TestTheWheelMovesMostOfAView(t *testing.T) {
	for dir, want := range map[string][2]float64{"down": {0, 640}, "": {0, 640}, "up": {0, -640}, "left": {-800, 0}, "right": {800, 0}} {
		dx, dy := wheelDelta(dir, 1000, 800)
		if dx != want[0] || dy != want[1] {
			t.Errorf("%q: %v,%v", dir, dx, dy)
		}
	}
}

func TestFoundReadsAsLines(t *testing.T) {
	f := &Found{Total: 3, Matches: []Match{
		{Text: "…Running shoes <light>…", In: &Control{Ref: "e7", Role: "link", Name: "Running shoes"}},
		{Text: "Walking boots", Near: []Control{{Ref: "e9", Role: "button", Name: "Add"}, {Ref: "e10", Role: "link"}}},
	}}
	got := findText("shoes", f)
	want := "- [3 matches for \"shoes\"; the first 2]\n" +
		"- text \"…Running shoes <light>…\" in link \"Running shoes\" [ref=e7]\n" +
		"- text \"Walking boots\" near button \"Add\" [ref=e9], link [ref=e10]"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
	if got := findText("x", &Found{}); !strings.Contains(got, "no visible text matches") {
		t.Fatal(got)
	}
	if got := findText("x", &Found{Total: 1, Matches: []Match{{Text: "x"}}}); !strings.HasPrefix(got, "- [1 match for") {
		t.Fatal(got)
	}
}

func TestTheDifferenceLeavesTheFocusOut(t *testing.T) {
	before := []string{"- button \"Add\" [ref=e1] [focused]", "- text \"Cart (0)\""}
	after := []string{"- button \"Add\" [ref=e1]", "- text \"Cart (1)\""}
	if got := diff(before, after, 2000); got != "+ - text \"Cart (1)\"\n- - text \"Cart (0)\"" {
		t.Fatalf("%q", got)
	}
}
