//go:build unix

package rpc_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/wire"
	"github.com/ascorblack/daedalus/browserd/internal/workflow"
)

// person is the operator at the live view: pointing at elements by where the page draws them, and
// typing as the app sends typing (text as text, keys as keys).
type person struct {
	t   *testing.T
	h   *harness
	v   *viewConn
	tab string
	at  map[string][2]float64
}

// driving opens the steps page, takes the browser for a live view, and measures where each named
// element is, as the operator's eyes would.
func driving(t *testing.T, h *harness) *person {
	t.Helper()
	o := h.open("g1", "project-a", "/site/steps.html")
	p := &person{t: t, h: h, tab: o.Tab.ID, at: map[string][2]float64{}}
	snap := h.snapshot(o.Tab.ID)
	for _, e := range [][2]string{
		{"textbox", "Search"}, {"combobox", "Period"}, {"checkbox", "Include archived"}, {"button", "Show"},
		{"textbox", "Comment"}, {"textbox", "Email"}, {"textbox", "Password"}, {"button", "Sign in"},
		{"textbox", "Card number"}, {"textbox", "Code"}, {"link", "Export CSV"}, {"button", "Place order"},
		{"link", "Download report"}, {"button", "Reset"}, {"combobox", "Birth year"},
	} {
		ref := refOf(t, snap, e[0], e[1])
		r := h.mustAct(o.Tab.ID, map[string]any{"action": "click", "ref": ref, "element": e[1], "dry_run": true,
			"origin": map[string]any{"actor": "operator"}})
		p.at[e[1]] = [2]float64{r.Box.X + r.Box.W/2, r.Box.Y + r.Box.H/2}
	}
	p.v = h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	p.v.waitEvent("hello", 10*time.Second, nil)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": p.v.id}, nil)
	p.v.waitEvent("control", 5*time.Second, func(e map[string]any) bool { return e["holder"] == "you" })
	return p
}

func (p *person) click(name string) {
	pt, ok := p.at[name]
	if !ok {
		p.t.Fatalf("no element %q measured", name)
	}
	for _, typ := range []string{"move", "down", "up"} {
		p.v.input(map[string]any{"t": "mouse", "type": typ, "x": pt[0], "y": pt[1], "button": "left", "clicks": 1})
	}
}

func (p *person) text(s string) { p.v.input(map[string]any{"t": "text", "text": s}) }

func (p *person) key(key, code string, keyCode int) {
	down := map[string]any{"t": "key", "type": "down", "key": key, "code": code, "key_code": keyCode, "mods": 0}
	if key == "Enter" {
		down["text"] = "\r"
	}
	p.v.input(down)
	p.v.input(map[string]any{"t": "key", "type": "up", "key": key, "code": code, "key_code": keyCode, "mods": 0})
}

// settle waits until the inputs sent so far were dispatched: the view answers a copy in input order.
func (p *person) settle() {
	id := "s" + time.Now().Format("150405.000000000")
	p.v.input(map[string]any{"t": "copy", "id": id})
	p.v.waitEvent("copied", 10*time.Second, func(e map[string]any) bool { return e["id"] == id })
}

func (p *person) waitText(want string) {
	p.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		var r struct {
			Text string `json:"text"`
		}
		p.h.must("page.text", map[string]any{"tab_id": p.tab, "origin": map[string]any{"actor": "operator"}}, &r)
		if strings.Contains(r.Text, want) {
			return
		}
		if time.Now().After(deadline) {
			p.t.Fatalf("the page never said %q: %s", want, r.Text)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func stepsOf(t *testing.T, wf workflow.Workflow, action string) []workflow.Step {
	t.Helper()
	var out []workflow.Step
	for _, s := range wf.Steps {
		if s.Action == action {
			out = append(out, s)
		}
	}
	return out
}

func TestARecordingIsTheOperatorsStepsInTheAgentsWordsAndNoSecret(t *testing.T) {
	h := start(t, nil)
	p := driving(t, h)
	var started workflow.Workflow
	h.must("workflow.start", map[string]any{"group_id": "g1", "values": "literal"}, &started)
	if started.State != "recording" || started.StartURL == "" || !strings.HasSuffix(started.StartURL, "/site/steps.html") || started.StartTitle != "Reports" {
		t.Fatalf("started: %+v", started)
	}
	p.v.waitEvent("workflow", 5*time.Second, func(e map[string]any) bool { return e["state"] == "started" })

	// A search typed and sent with Enter: one step, the click into the box folded into it.
	p.click("Search")
	p.text("blue shoes")
	p.key("Enter", "Enter", 13)
	p.waitText("found blue shoes")
	// A list reached with Tab and changed with the arrows, a box ticked, a button pressed.
	p.key("Tab", "Tab", 9)
	p.key("ArrowDown", "ArrowDown", 40)
	p.click("Include archived")
	p.click("Show")
	// A comment with a telephone number: typed, but never kept, even with values allowed.
	p.click("Comment")
	p.text("call 555 123 4567")
	// A sign-in: the name beside the password, the password, the button. None of it is kept.
	p.click("Email")
	p.text("someone@example.com")
	p.click("Password")
	p.text("hunter2-SECRET")
	p.click("Sign in")
	// A card and a code, by the field's own words (the code has no autocomplete).
	p.click("Card number")
	p.text("4111111111111111")
	p.click("Code")
	p.text("SECRET-492817")
	// A purchase, then the export link, whose address holds a token.
	p.click("Place order")
	p.settle()
	p.waitText("ordered")
	p.click("Export CSV")
	p.v.waitEvent("workflow", 10*time.Second, func(e map[string]any) bool {
		st, _ := e["step"].(map[string]any)
		return st != nil && st["action"] == "arrive"
	})

	var mark struct {
		Step workflow.Step `json:"step"`
	}
	h.must("workflow.mark", map[string]any{"group_id": "g1"}, &mark)
	if mark.Step.Action != "expect" || mark.Step.Title != "Reports" {
		t.Fatalf("mark: %+v", mark.Step)
	}
	p.v.waitEvent("workflow", 5*time.Second, func(e map[string]any) bool { return e["state"] == "step" })

	// Giving the browser back ends the recording, which is published whole.
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "agent"}, nil)
	var wf workflow.Workflow
	deadline := time.Now().Add(10 * time.Second)
	for {
		var got struct {
			Workflow *workflow.Workflow `json:"workflow"`
		}
		h.must("workflow.get", map[string]any{"group_id": "g1"}, &got)
		if got.Workflow != nil && got.Workflow.State == "stopped" {
			wf = *got.Workflow
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the recording did not stop with the give-back: %+v", got.Workflow)
		}
		time.Sleep(100 * time.Millisecond)
	}
	raw, _ := json.MarshalIndent(wf, "", " ")
	t.Logf("recording:\n%s", raw)
	if wf.Reason != "control" {
		t.Fatalf("stopped for %q, not the give-back", wf.Reason)
	}

	// Nothing secret, personal or long-numbered is anywhere in it, nor any length of it.
	for _, secret := range []string{"hunter2", "someone@example.com", "4111", "492817", "555 123", "SECRETTOKEN", "text_len", "length"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("the recording holds %q", secret)
		}
	}
	events, _ := h.evlog.After(0, 20000)
	for _, e := range events {
		b, _ := json.Marshal(e.Data)
		for _, secret := range []string{"hunter2", "someone@example.com", "4111111111111111", "492817"} {
			if strings.Contains(string(b), secret) {
				t.Fatalf("event %s holds %q: %s", e.Type, secret, b)
			}
		}
	}

	types := stepsOf(t, wf, "type")
	if len(types) != 2 {
		t.Fatalf("type steps: %+v", types)
	}
	search := types[0]
	if search.Element == nil || search.Element.Name != "Search" || search.Element.Role != "textbox" || search.Slot != "search" ||
		!search.Submit || search.Value == nil || *search.Value != "blue shoes" || search.Element.Place != "Filters" {
		t.Fatalf("the search step: %+v %+v", search, search.Element)
	}
	if wf.Steps[0].Action != "type" {
		t.Fatalf("the click into the search box was not folded into its typing: %+v", wf.Steps[0])
	}
	comment := types[1]
	if comment.Slot != "comment" || comment.Value != nil {
		t.Fatalf("the comment step kept its telephone number: %+v", comment)
	}
	if sel := stepsOf(t, wf, "select"); len(sel) != 1 || sel[0].Option != "Last month" || sel[0].Element.Name != "Period" {
		t.Fatalf("select steps: %+v", sel)
	}
	if ch := stepsOf(t, wf, "check"); len(ch) != 1 || ch[0].Element.Name != "Include archived" {
		t.Fatalf("check steps: %+v", ch)
	}
	handoffs := stepsOf(t, wf, "handoff")
	reasons := []string{}
	for _, s := range handoffs {
		reasons = append(reasons, s.Reason)
	}
	if strings.Join(reasons, ",") != "login,payment,two_factor" {
		t.Fatalf("handoffs %v: %+v", reasons, handoffs)
	}
	clicks := stepsOf(t, wf, "click")
	names := []string{}
	for _, c := range clicks {
		names = append(names, c.Element.Name)
		if c.Element.Name == "Email" || c.Element.Name == "Password" || c.Element.Name == "Sign in" {
			t.Fatalf("a click in the sign-in was recorded as a step of its own: %+v", c)
		}
	}
	if strings.Join(names, ",") != "Show,Place order,Export CSV" {
		t.Fatalf("clicks: %v", names)
	}
	order := clicks[1]
	if len(order.Asks) == 0 || order.Asks[0] != "purchase" {
		t.Fatalf("the order's press does not say it is asked about: %+v", order)
	}
	export := clicks[2]
	if !strings.Contains(export.To, "token={token}") || !strings.Contains(export.To, "keyword=shoes") || strings.Contains(export.To, "#") {
		t.Fatalf("the link's address: %q", export.To)
	}
	arrivals := stepsOf(t, wf, "arrive")
	if len(arrivals) == 0 || !strings.Contains(arrivals[len(arrivals)-1].To, "token={token}") {
		t.Fatalf("arrivals: %+v", arrivals)
	}
	last := wf.Steps[len(wf.Steps)-1]
	if last.Action != "expect" {
		t.Fatalf("the last step is %+v, not the mark", last)
	}
	for i, s := range wf.Steps {
		if s.N != i+1 {
			t.Fatalf("step %d is numbered %d", i+1, s.N)
		}
	}
}

func TestARecordingKeepsNoTypedValueUnlessAllowed(t *testing.T) {
	h := start(t, nil)
	p := driving(t, h)
	h.must("workflow.start", map[string]any{"group_id": "g1"}, nil)
	p.click("Search")
	p.text("blue shoes")
	p.key("Tab", "Tab", 9)
	p.settle()
	var wf workflow.Workflow
	h.must("workflow.stop", map[string]any{"group_id": "g1"}, &wf)
	if wf.Values != "slots" || wf.Reason != "operator" {
		t.Fatalf("stopped: %+v", wf)
	}
	types := stepsOf(t, wf, "type")
	if len(types) != 1 || types[0].Slot != "search" || types[0].Value != nil || types[0].Submit {
		t.Fatalf("type steps: %+v", types)
	}
	raw, _ := json.Marshal(wf)
	if strings.Contains(string(raw), "blue") {
		t.Fatalf("the recording kept the text: %s", raw)
	}
}

func TestOnlyAPersonDrivingIsRecorded(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/steps.html")
	if err := h.call("workflow.start", map[string]any{"group_id": "g1"}, nil); code(err) != 1004 {
		t.Fatalf("a recording started while the agent drives: %v", err)
	}
	if err := h.call("workflow.start", map[string]any{"group_id": "g1", "values": "everything"}, nil); code(err) != -32602 {
		t.Fatalf("an unknown values mode: %v", err)
	}
	if err := h.call("workflow.stop", map[string]any{"group_id": "g1"}, nil); code(err) != 1001 {
		t.Fatalf("stopping what never started: %v", err)
	}
	// An action through page.act (the agent's, or the toolbar's) is not a person's input: not a step.
	ref := refOf(t, h.snapshot(o.Tab.ID), "button", "Show")
	p := driving(t, h)
	h.must("workflow.start", map[string]any{"group_id": "g1"}, nil)
	var again workflow.Workflow
	h.must("workflow.start", map[string]any{"group_id": "g1", "values": "literal"}, &again)
	if again.Values != "slots" {
		t.Fatalf("a second start replaced the recording: %+v", again)
	}
	h.mustAct(o.Tab.ID, map[string]any{"action": "click", "ref": ref, "element": "Show", "origin": map[string]any{"actor": "operator"}})
	p.click("Include archived")
	p.settle()
	var wf workflow.Workflow
	h.must("workflow.stop", map[string]any{"group_id": "g1"}, &wf)
	if len(wf.Steps) != 1 || wf.Steps[0].Action != "check" {
		t.Fatalf("steps: %+v", wf.Steps)
	}
}

func TestARecordingFollowsTheAddressBarKeysScrollingDialogsAndDownloads(t *testing.T) {
	h := start(t, nil)
	p := driving(t, h)
	h.must("workflow.start", map[string]any{"group_id": "g1"}, nil)
	step := func(action string) map[string]any {
		e := p.v.waitEvent("workflow", 10*time.Second, func(e map[string]any) bool {
			st, _ := e["step"].(map[string]any)
			return st != nil && st["action"] == action
		})
		return e["step"].(map[string]any)
	}
	// Escape twice is one step pressed twice; three turns of the wheel are one scroll.
	p.key("Escape", "Escape", 27)
	p.key("Escape", "Escape", 27)
	for i := 0; i < 3; i++ {
		p.v.input(map[string]any{"t": "wheel", "x": 600, "y": 400, "dx": 0, "dy": 120})
	}
	p.v.input(map[string]any{"t": "wheel", "x": 600, "y": 400, "dx": 0, "dy": -120})
	p.v.input(map[string]any{"t": "wheel", "x": 600, "y": 400, "dx": 0, "dy": 120})
	p.settle()
	// The page's confirm, answered by the operator.
	p.click("Reset")
	p.settle()
	deadline := time.Now().Add(5 * time.Second)
	for {
		err := h.call("dialog.answer", map[string]any{"tab_id": p.tab, "accept": true, "origin": map[string]any{"actor": "operator"}}, nil)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("no dialog to answer: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	p.waitText("reset")
	// A download, then the address bar with a session id in the address.
	p.click("Download report")
	step("download")
	p.v.input(map[string]any{"t": "nav", "action": "url", "url": h.site.URL + "/site/steps.html?sid=abc&view=table"})
	step("arrive")
	p.v.input(map[string]any{"t": "nav", "action": "back"})
	p.settle()
	var wf workflow.Workflow
	h.must("workflow.stop", map[string]any{"group_id": "g1"}, &wf)
	raw, _ := json.MarshalIndent(wf.Steps, "", " ")
	t.Logf("steps:\n%s", raw)
	want := []string{"press", "scroll", "scroll", "scroll", "click", "dialog", "click", "download", "navigate", "arrive", "navigate"}
	var got []string
	for _, s := range wf.Steps {
		if s.Action == "arrive" && len(got) > 0 && got[len(got)-1] == "arrive" {
			continue
		}
		got = append(got, s.Action)
	}
	if strings.Join(got[:min(len(got), len(want))], ",") != strings.Join(want, ",") {
		t.Fatalf("steps %v, want %v first", got, want)
	}
	s := wf.Steps
	if s[0].Keys != "Escape" || s[0].Count != 2 {
		t.Fatalf("the presses: %+v", s[0])
	}
	if s[1].Direction != "down" || s[1].Count != 3 || s[2].Direction != "up" || s[3].Direction != "down" || s[3].Count > 1 {
		t.Fatalf("the scrolls: %+v %+v %+v", s[1], s[2], s[3])
	}
	if s[5].Kind != "confirm" || s[5].Text != "Reset the filters?" || s[5].Accept == nil || !*s[5].Accept {
		t.Fatalf("the dialog: %+v", s[5])
	}
	if s[7].Text != "report.txt" {
		t.Fatalf("the download: %+v", s[7])
	}
	if !strings.HasSuffix(s[8].To, "/site/steps.html?sid={sid}&view=table") {
		t.Fatalf("the address typed: %q", s[8].To)
	}
	if last := s[len(s)-1]; last.Action != "navigate" || last.Go != "back" {
		t.Fatalf("the last step: %+v", last)
	}
}

func TestAPersonalChoiceIsABlankEvenWithValuesKept(t *testing.T) {
	h := start(t, nil)
	p := driving(t, h)
	h.must("workflow.start", map[string]any{"group_id": "g1", "values": "literal"}, nil)
	// Reached with Tab, as a list is: a click on one opens Chromium's own drop-down, which a
	// headless browser draws nowhere the live view shows.
	p.click("Place order")
	p.key("Tab", "Tab", 9)
	p.key("ArrowDown", "ArrowDown", 40)
	p.click("Show")
	p.settle()
	var wf workflow.Workflow
	h.must("workflow.stop", map[string]any{"group_id": "g1"}, &wf)
	sel := stepsOf(t, wf, "select")
	if len(sel) != 1 || sel[0].Option != "" || sel[0].Slot != "birth_year" {
		t.Fatalf("the birth year's step: %+v in %+v", sel, wf.Steps)
	}
	raw, _ := json.Marshal(wf)
	if strings.Contains(string(raw), "1981") {
		t.Fatalf("the recording kept the year: %s", raw)
	}
}
