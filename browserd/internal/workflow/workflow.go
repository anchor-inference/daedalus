// Package workflow records how a person solves a task in the agent's browser, as steps in the
// agent's own words: open this address, click the element with this role and name, type into this
// field, choose this option, press this key. The host turns a finished recording into a procedure
// the operator approves, and an agent follows it with its own tools, under its own rules.
//
// It is the operator's: it records only while a person holds the browser and only once they
// started it, and it stops the moment the browser goes back to the agent. What it never keeps is
// what the agent may never type: a password, a one-time code, a card, and the name typed beside a
// password, which become one step the operator does themselves (a handoff), without a character or
// a length of what was typed. A value typed into an ordinary field is a named blank; it is kept as
// it is only when the operator allowed values to be kept and it looks like nothing personal.
package workflow

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
	"github.com/ascorblack/daedalus/ptyd/proto/events"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// MaxSteps is the most one recording holds; at it, the recording stops (reason "full"). A task
// longer than this is several procedures.
const MaxSteps = 200

// inputBudget bounds what reading the page costs one of a person's inputs: their click waits for it.
const inputBudget = 1500 * time.Millisecond

// Values says what a recording keeps of the text typed into ordinary fields.
const (
	Slots   = "slots"   // a named blank, nothing of the text (the default)
	Literal = "literal" // the text itself where it looks like nothing personal, else a blank
)

// Element is what a step acted on, as the agent's snapshot names it.
type Element struct {
	Role  string `json:"role"`
	Name  string `json:"name"`
	Place string `json:"place,omitempty"`
}

// Step is one thing the person did, in the vocabulary of the agent's tools. The fields a step has
// depend on its action (docs/architecture/browser.md, Recording a person's steps).
type Step struct {
	N         int      `json:"n"`
	At        int64    `json:"at"`
	Tab       string   `json:"tab"`
	URL       string   `json:"url"`
	Action    string   `json:"action"`
	Element   *Element `json:"element,omitempty"`
	Slot      string   `json:"slot,omitempty"`
	Value     *string  `json:"value,omitempty"`
	Submit    bool     `json:"submit,omitempty"`
	Option    string   `json:"option,omitempty"`
	Keys      string   `json:"keys,omitempty"`
	Count     int      `json:"count,omitempty"`
	Direction string   `json:"direction,omitempty"`
	Reason    string   `json:"reason,omitempty"`
	To        string   `json:"to,omitempty"`
	Go        string   `json:"go,omitempty"`
	Title     string   `json:"title,omitempty"`
	Text      string   `json:"text,omitempty"`
	Kind      string   `json:"kind,omitempty"`
	Accept    *bool    `json:"accept,omitempty"`
	Asks      []string `json:"asks,omitempty"`
	Point     []int    `json:"point,omitempty"`

	key string // the element's identity in its document, to fold a click into the typing after it
}

// Workflow is one recording.
type Workflow struct {
	ID         string `json:"id"`
	GroupID    string `json:"group_id"`
	Values     string `json:"values"`
	State      string `json:"state"`            // "recording" or "stopped"
	Reason     string `json:"reason,omitempty"` // why it stopped: operator, control, closed, full
	StartedAt  int64  `json:"started_at"`
	StoppedAt  int64  `json:"stopped_at,omitempty"`
	StartURL   string `json:"start_url"`
	StartTitle string `json:"start_title"`
	Steps      []Step `json:"steps"`
}

// Pages is what the recorder reads of pages: the page model's recording functions.
type Pages interface {
	TargetAt(ctx context.Context, t *browser.Tab, x, y float64) (*page.Target, error)
	FocusedTarget(ctx context.Context, t *browser.Tab) (*page.Target, error)
	TargetValue(ctx context.Context, tg *page.Target) (page.Value, error)
	Selection(ctx context.Context, t *browser.Tab, max int) (text string, truncated, withheld bool, err error)
	Headline(ctx context.Context, t *browser.Tab) (string, error)
}

// Recorder records the groups whose operator started a recording.
type Recorder struct {
	Pages   Pages
	Groups  func(id string) (*browser.Group, error)
	Publish func(typ string, data any)
	Events  *events.Log
	Log     *slog.Logger

	mu   sync.Mutex
	on   map[string]*session
	last map[string]*Workflow
}

// watch is the field a person is typing into, until they are done with it.
type watch struct {
	tg     *page.Target
	before page.Value
	typed  bool
	// secret is a field that is the operator's (a password, a code, a card, a sign-in's name):
	// nothing of it is read, and being done with it records nothing more.
	secret bool
}

type touch struct {
	x, y   float64
	dy, dx float64
	moved  bool
	tg     *page.Target
}

type session struct {
	mu      sync.Mutex
	wf      *Workflow
	stopped bool
	tab     string
	field   *watch
	touch   *touch
	slots   map[string]string // field key -> its blank
	taken   map[string]bool   // blanks in use
	arrived map[string]string // tab -> the address its last arrival was recorded at
	dialog  map[string]*browser.Dialog
}

func (r *Recorder) session(group string) *session {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.on[group]
}

func now() int64 { return time.Now().UnixMilli() }

// Start begins recording group g. Only a person's own steps are recorded, so the browser must be
// theirs; a recording already on is returned as it is.
func (r *Recorder) Start(g *browser.Group, values string) (*Workflow, error) {
	switch values {
	case "":
		values = Slots
	case Slots, Literal:
	default:
		return nil, protowire.Errorf(protowire.CodeInvalidParams, "values must be slots or literal")
	}
	if g.Control().Owner != browser.OwnerHuman {
		return nil, protowire.Errorf(protowire.CodeForbidden, "take the browser first: a recording is of a person's own steps")
	}
	r.mu.Lock()
	if r.on == nil {
		r.on, r.last = map[string]*session{}, map[string]*Workflow{}
	}
	if s := r.on[g.ID]; s != nil {
		// The session's own lock is taken without the recorder's: a step that fills the recording
		// holds the session's and then takes the recorder's.
		r.mu.Unlock()
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.snapshot(false), nil
	}
	defer r.mu.Unlock()
	raw := make([]byte, 4)
	_, _ = rand.Read(raw)
	wf := &Workflow{ID: "w" + hex.EncodeToString(raw), GroupID: g.ID, Values: values, State: "recording", StartedAt: now(), Steps: []Step{}}
	s := &session{wf: wf, slots: map[string]string{}, taken: map[string]bool{}, arrived: map[string]string{}, dialog: map[string]*browser.Dialog{}}
	for _, t := range g.Tabs() {
		s.arrived[t.ID] = CleanURL(t.URL())
	}
	if t := g.ActiveTab(); t != nil {
		wf.StartURL, wf.StartTitle = CleanURL(t.URL()), words(t.Title(), maxTitle)
		s.tab = t.ID
	}
	r.on[g.ID] = s
	r.publish("workflow.started", map[string]any{"group_id": g.ID, "workflow": s.snapshot(true)})
	return s.snapshot(false), nil
}

// Stop ends group's recording and returns it whole; reason is why (operator, control, closed, full).
func (r *Recorder) Stop(group, reason string) (*Workflow, error) {
	r.mu.Lock()
	s := r.on[group]
	delete(r.on, group)
	r.mu.Unlock()
	if s == nil {
		return nil, protowire.Errorf(protowire.CodeNotFound, "group %q is not recording", group)
	}
	ctx, cancel := context.WithTimeout(context.Background(), inputBudget)
	defer cancel()
	s.mu.Lock()
	r.commit(ctx, s, false)
	wf := r.finish(s, reason)
	s.mu.Unlock()
	return wf, nil
}

// finish marks a session stopped and publishes the whole recording; s.mu is held.
func (r *Recorder) finish(s *session, reason string) *Workflow {
	s.stopped = true
	s.wf.State, s.wf.Reason, s.wf.StoppedAt = "stopped", reason, now()
	wf := s.snapshot(false)
	r.mu.Lock()
	if r.last == nil {
		r.last = map[string]*Workflow{}
	}
	if r.on[wf.GroupID] == s {
		delete(r.on, wf.GroupID)
	}
	r.last[wf.GroupID] = wf
	r.mu.Unlock()
	r.publish("workflow.stopped", map[string]any{"group_id": wf.GroupID, "workflow": wf})
	return wf
}

// Get is the group's recording now, or the last one it made.
func (r *Recorder) Get(group string) *Workflow {
	if s := r.session(group); s != nil {
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.snapshot(false)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.last[group]
}

// Forget drops what is kept of a closed group.
func (r *Recorder) Forget(group string) {
	if r.session(group) != nil {
		_, _ = r.Stop(group, "closed")
	}
	r.mu.Lock()
	delete(r.last, group)
	r.mu.Unlock()
}

// snapshot copies the workflow; bare leaves the steps out. s.mu is held.
func (s *session) snapshot(bare bool) *Workflow {
	wf := *s.wf
	if bare {
		wf.Steps = nil
	} else {
		wf.Steps = append([]Step{}, s.wf.Steps...)
	}
	return &wf
}

// stepped publishes a step: a new one, or, with replaces, the step of that number as it now is.
func (r *Recorder) stepped(s *session, st Step, replaces int) {
	data := map[string]any{"group_id": s.wf.GroupID, "id": s.wf.ID, "step": st, "steps": len(s.wf.Steps)}
	if replaces > 0 {
		data["replaces"] = replaces
	}
	r.publish("workflow.step", data)
}

// Summary is the recording without its steps, as a live view is told of it.
func (w *Workflow) Summary() map[string]any {
	return map[string]any{"id": w.ID, "values": w.Values, "recording": w.State == "recording", "steps": len(w.Steps),
		"reason": w.Reason, "started_at": w.StartedAt}
}

func (r *Recorder) publish(typ string, data any) {
	if r.Publish != nil {
		r.Publish(typ, data)
	}
}

// add appends a step, folding it into the last one where they are one thing: presses of the same
// key, scrolls the same way, a handoff for the same reason. s.mu is held.
func (r *Recorder) add(s *session, t *browser.Tab, st Step) {
	if s.stopped {
		return
	}
	st.At = now()
	if t != nil {
		st.Tab, st.URL = t.ID, CleanURL(t.URL())
	}
	if n := len(s.wf.Steps); n > 0 {
		last := &s.wf.Steps[n-1]
		same := last.Action == st.Action && last.Tab == st.Tab
		switch {
		case same && st.Action == "press" && last.Keys == st.Keys,
			same && st.Action == "scroll" && last.Direction == st.Direction:
			last.Count = max(1, last.Count) + 1
			last.At = st.At
			r.stepped(s, *last, len(s.wf.Steps))
			return
		case same && st.Action == "handoff" && last.Reason == st.Reason && last.URL == st.URL:
			return
		}
	}
	st.N = len(s.wf.Steps) + 1
	s.wf.Steps = append(s.wf.Steps, st)
	r.stepped(s, st, 0)
	if len(s.wf.Steps) >= MaxSteps {
		r.finish(s, "full")
	}
}

// replaceLast puts st in place of the last step (a click into a field, now the typing it began).
// s.mu is held.
func (r *Recorder) replaceLast(s *session, t *browser.Tab, st Step) {
	n := len(s.wf.Steps)
	st.At, st.N = now(), n
	if t != nil {
		st.Tab, st.URL = t.ID, CleanURL(t.URL())
	}
	s.wf.Steps[n-1] = st
	r.stepped(s, st, n)
}

func element(tg *page.Target) *Element {
	f := tg.Field
	return &Element{Role: clip(f.Role, 40), Name: words(f.Name, maxName), Place: words(f.Place, maxPlace)}
}

// handoff is the reason a target is the operator's to fill, or "": a secret field by its nature, a
// field of a sign-in (beside a password or a code), or a press that sends one.
func handoff(tg *page.Target) string {
	reason := func(kind string) string {
		switch kind {
		case "one_time_code":
			return "two_factor"
		case "payment":
			return "payment"
		}
		return "login"
	}
	f := tg.Field
	if f.Secret != "" {
		return reason(f.Secret)
	}
	if (f.Text || f.Select) && (f.Near == "password" || f.Near == "one_time_code") {
		return reason(f.Near)
	}
	// A press that sends a sign-in. The classifier's own word for it counts only beside a field
	// that is secret by its nature: it also counts the fields a person typed into as secret, which
	// would make every form of a recording a sign-in.
	if f.Near == "" {
		return ""
	}
	for _, k := range tg.Sensitive {
		if k == "credentials" {
			return reason(f.Near)
		}
	}
	return ""
}

// asks are the kinds of a press the agent's own would be asked about, less the sign-in, which is a
// handoff: a purchase, a message sent, something deleted.
func asks(tg *page.Target) []string {
	var out []string
	for _, k := range tg.Sensitive {
		if k != "credentials" {
			out = append(out, k)
		}
	}
	return out
}

// slot is the blank a field's text becomes: named from the field, the same for the same field.
// s.mu is held.
func (s *session) slot(tg *page.Target) string {
	if v, ok := s.slots[tg.Key]; ok {
		return v
	}
	base := slotBase(tg.Field.Name, "text")
	name := base
	for i := 2; s.taken[name]; i++ {
		name = base + "_" + itoa(i)
	}
	s.taken[name] = true
	s.slots[tg.Key] = name
	return name
}

func itoa(i int) string {
	var b [20]byte
	n := len(b)
	for {
		n--
		b[n] = byte('0' + i%10)
		i /= 10
		if i == 0 {
			return string(b[n:])
		}
	}
}

// Before is told of each of a person's inputs before it is dispatched, while the page is still as
// they saw it: a press is described by what is under it now, not by what the press made of the page.
func (r *Recorder) Before(t *browser.Tab, in wire.Input) {
	s := r.session(t.Group.ID)
	if s == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), inputBudget)
	defer cancel()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped {
		return
	}
	if s.tab != "" && s.tab != t.ID && in.T != "copy" {
		r.commit(ctx, s, false)
		r.add(s, t, Step{Action: "tab", To: CleanURL(t.URL()), Title: words(t.Title(), maxTitle)})
	}
	s.tab = t.ID
	switch in.T {
	case "mouse":
		if in.Type != "down" {
			return
		}
		if in.Clicks >= 2 {
			// The second press of a double click: the first was recorded as a click on the same place.
			if n := len(s.wf.Steps); n > 0 && s.wf.Steps[n-1].Action == "click" {
				s.wf.Steps[n-1].Action = "double_click"
				r.stepped(s, s.wf.Steps[n-1], n)
				return
			}
		}
		action := "click"
		if in.Button == "right" {
			action = "right_click"
		}
		r.commit(ctx, s, false)
		tg, err := r.Pages.TargetAt(ctx, t, in.X, in.Y)
		if err != nil || tg == nil {
			r.add(s, t, Step{Action: action, Point: []int{int(math.Round(in.X)), int(math.Round(in.Y))}})
			return
		}
		r.press(ctx, s, t, tg, action)
	case "touch":
		r.touchInput(ctx, s, t, in)
	case "wheel":
		dir := ""
		switch {
		case math.Abs(in.DY) >= math.Abs(in.DX) && in.DY > 0:
			dir = "down"
		case math.Abs(in.DY) >= math.Abs(in.DX) && in.DY < 0:
			dir = "up"
		case in.DX > 0:
			dir = "right"
		case in.DX < 0:
			dir = "left"
		}
		if dir != "" {
			r.add(s, t, Step{Action: "scroll", Direction: dir})
		}
	case "text":
		// Text typed with nothing to type into goes nowhere; there is no step in it.
		r.typing(ctx, s, t)
	case "key":
		r.key(ctx, s, t, in)
	case "nav":
		r.commit(ctx, s, false)
		st := Step{Action: "navigate"}
		if in.Action == "url" {
			st.To = CleanURL(in.URL)
		} else {
			st.Go = in.Action
		}
		r.add(s, t, st)
	}
}

// press records a click on tg. s.mu is held.
func (r *Recorder) press(ctx context.Context, s *session, t *browser.Tab, tg *page.Target, action string) {
	if reason := handoff(tg); reason != "" {
		r.add(s, t, Step{Action: "handoff", Reason: reason, Element: element(tg)})
		if tg.Field.Text || tg.Field.Select {
			s.field = &watch{tg: tg, secret: true}
		}
		return
	}
	if tg.Field.Check && action == "click" {
		action = "check"
		if tg.Field.Checked && tg.Field.Type != "radio" && tg.Field.Role != "radio" {
			action = "uncheck"
		}
	}
	st := Step{Action: action, Element: element(tg), Asks: asks(tg), key: tg.Key}
	if tg.Field.Href != "" && tg.Field.Role == "link" {
		st.To = CleanURL(tg.Field.Href)
	}
	r.add(s, t, st)
	if (tg.Field.Text || tg.Field.Select) && action == "click" {
		before, err := r.Pages.TargetValue(ctx, tg)
		if err == nil {
			s.field = &watch{tg: tg, before: before}
		}
	}
}

// typing notes that the person types; the field is described the first time. It says whether there
// is a field to type into. s.mu is held.
func (r *Recorder) typing(ctx context.Context, s *session, t *browser.Tab) bool {
	if s.field != nil {
		s.field.typed = true
		return true
	}
	tg, err := r.Pages.FocusedTarget(ctx, t)
	if err != nil || tg == nil {
		return false
	}
	if reason := handoff(tg); reason != "" {
		r.add(s, t, Step{Action: "handoff", Reason: reason, Element: element(tg)})
		s.field = &watch{tg: tg, secret: true}
		return true
	}
	if !tg.Field.Text && !tg.Field.Select {
		return false
	}
	before, err := r.Pages.TargetValue(ctx, tg)
	if err != nil {
		return false
	}
	s.field = &watch{tg: tg, before: before, typed: true}
	return true
}

// commit records what the person did to the field they are done with: the text typed (as a blank,
// or as itself where allowed), or the option chosen. It says whether a step was recorded. s.mu is
// held.
func (r *Recorder) commit(ctx context.Context, s *session, submit bool) bool {
	w := s.field
	s.field = nil
	if w == nil || w.secret {
		return false
	}
	v, err := r.Pages.TargetValue(ctx, w.tg)
	if err != nil {
		if !w.typed || !w.tg.Field.Text {
			return false
		}
		// The page moved on before its field could be read: the step is still the person's.
		v = page.Value{Value: "\x00"}
	}
	if v.Withheld {
		return false
	}
	var st Step
	switch {
	case w.tg.Field.Select:
		if v.Option == w.before.Option {
			return false
		}
		st = Step{Action: "select", Element: element(w.tg), Option: words(v.Option, maxText), key: w.tg.Key}
		if w.tg.Field.Personal {
			// A birth year or a country chosen from a list is the person's, like one typed.
			st.Option, st.Slot = "", s.slot(w.tg)
		}
	case w.tg.Field.Text:
		if v.Value == w.before.Value {
			return false
		}
		st = Step{Action: "type", Element: element(w.tg), Slot: s.slot(w.tg), Submit: submit, key: w.tg.Key}
		if s.wf.Values == Literal && v.Value != "\x00" && !w.tg.Field.Personal && SafeLiteral(v.Value) {
			value := strings.TrimSpace(v.Value)
			st.Value = &value
		}
	default:
		return false
	}
	var t *browser.Tab
	if g, err := r.Groups(s.wf.GroupID); err == nil {
		for _, x := range g.Tabs() {
			if strings.HasPrefix(w.tg.Key, x.ID+"/") {
				t = x
			}
		}
	}
	if n := len(s.wf.Steps); n > 0 && s.wf.Steps[n-1].Action == "click" && s.wf.Steps[n-1].key == st.key {
		r.replaceLast(s, t, st)
		return true
	}
	r.add(s, t, st)
	return true
}

// key records a key a person pressed: typing goes to the field it types into, the keys that edit a
// field are part of that typing, and the rest are presses. s.mu is held.
func (r *Recorder) key(ctx context.Context, s *session, t *browser.Tab, in wire.Input) {
	if in.Type != "down" {
		return
	}
	switch in.Key {
	case "Shift", "Control", "Alt", "Meta", "CapsLock", "Dead", "Unidentified", "Process":
		return
	}
	chordMods := in.Mods & (1 | 2 | 4)
	if in.Key == "Tab" && chordMods == 0 {
		// The focus moves on; the next typing names the field it lands in.
		r.commit(ctx, s, false)
		return
	}
	if in.Key == "Enter" && chordMods == 0 {
		switch {
		case s.field != nil && s.field.secret:
			// Enter in a sign-in's field sends the sign-in, which the handoff already is.
			s.field = nil
			return
		case s.field != nil && s.field.tg.Field.Multi:
			s.field.typed = true
			return
		case s.field != nil && s.field.tg.Field.Select:
			// Enter on a list chooses what the arrows moved to.
			r.commit(ctx, s, false)
			return
		case s.field != nil && s.field.tg.Field.Text:
			if r.commit(ctx, s, true) {
				return
			}
		case s.field == nil:
			if r.typing(ctx, s, t) {
				if s.field.secret {
					s.field = nil
					return
				}
				if s.field.tg.Field.Multi {
					return
				}
				if r.commit(ctx, s, true) {
					return
				}
			}
		}
		r.commit(ctx, s, false)
		r.add(s, t, Step{Action: "press", Keys: "Enter"})
		return
	}
	if (in.Key == "ArrowUp" || in.Key == "ArrowDown") && chordMods == 0 && s.field == nil {
		// A list reached with Tab changes its choice with the arrows; a text field's arrows move
		// through what it suggests, which is a press.
		if r.typing(ctx, s, t) && s.field != nil && !s.field.secret && s.field.tg.Field.Select {
			return
		}
	}
	if editing(in, s.field) {
		if s.field != nil {
			s.field.typed = true
			return
		}
		if (in.Key == "Backspace" || in.Key == "Delete") && r.typing(ctx, s, t) {
			return
		}
		if chordMods != 0 {
			return // Ctrl+A, Ctrl+C and the like outside a field change nothing a step would say
		}
	}
	if s.field != nil && s.field.secret {
		return
	}
	r.commit(ctx, s, false)
	r.add(s, t, Step{Action: "press", Keys: chord(in)})
}

// editing says a key edits text rather than asks the page for something: deleting, moving the caret
// in a field, the clipboard and undo.
func editing(in wire.Input, f *watch) bool {
	ctrl := in.Mods&(2|4) != 0
	if ctrl && len(in.Key) == 1 && strings.Contains("acvxzyACVXZY", in.Key) {
		return true
	}
	switch in.Key {
	case "Backspace", "Delete":
		return true
	case "ArrowLeft", "ArrowRight", "Home", "End":
		return f != nil
	case "ArrowUp", "ArrowDown", "PageUp", "PageDown":
		return f != nil && f.tg != nil && (f.tg.Field.Multi || f.tg.Field.Select)
	}
	return false
}

// chord names a key as page.act's press takes it: Ctrl+, Alt+, Meta+ and Shift+ before the key.
func chord(in wire.Input) string {
	var b strings.Builder
	if in.Mods&2 != 0 {
		b.WriteString("Ctrl+")
	}
	if in.Mods&1 != 0 {
		b.WriteString("Alt+")
	}
	if in.Mods&4 != 0 {
		b.WriteString("Meta+")
	}
	if in.Mods&8 != 0 && len([]rune(in.Key)) > 1 {
		b.WriteString("Shift+")
	}
	k := in.Key
	if k == " " {
		k = "Space"
	}
	b.WriteString(clip(k, 20))
	return b.String()
}

// touchInput turns a finger's tap into a click and its swipe into a scroll. s.mu is held.
func (r *Recorder) touchInput(ctx context.Context, s *session, t *browser.Tab, in wire.Input) {
	switch in.Type {
	case "start":
		if len(in.Points) != 1 {
			s.touch = nil
			return
		}
		p := in.Points[0]
		tg, _ := r.Pages.TargetAt(ctx, t, p.X, p.Y)
		s.touch = &touch{x: p.X, y: p.Y, tg: tg}
	case "move":
		if s.touch == nil || len(in.Points) == 0 {
			return
		}
		p := in.Points[0]
		s.touch.dx, s.touch.dy = p.X-s.touch.x, p.Y-s.touch.y
		if math.Hypot(s.touch.dx, s.touch.dy) > 12 {
			s.touch.moved = true
		}
	case "end":
		tc := s.touch
		s.touch = nil
		if tc == nil {
			return
		}
		if tc.moved {
			dir := "down"
			switch {
			case math.Abs(tc.dy) >= math.Abs(tc.dx) && tc.dy > 0:
				dir = "up" // a finger drawn down pulls the page's top into view
			case math.Abs(tc.dx) > math.Abs(tc.dy) && tc.dx > 0:
				dir = "left"
			case math.Abs(tc.dx) > math.Abs(tc.dy):
				dir = "right"
			}
			r.add(s, t, Step{Action: "scroll", Direction: dir})
			return
		}
		r.commit(ctx, s, false)
		if tc.tg == nil {
			r.add(s, t, Step{Action: "click", Point: []int{int(math.Round(tc.x)), int(math.Round(tc.y))}})
			return
		}
		r.press(ctx, s, t, tc.tg, "click")
	case "cancel":
		s.touch = nil
	}
}

// Mark records what the person points at as the sign the task is done: the text they selected on
// the page, else its heading.
func (r *Recorder) Mark(t *browser.Tab) (*Step, error) {
	s := r.session(t.Group.ID)
	if s == nil {
		return nil, protowire.Errorf(protowire.CodeNotFound, "group %q is not recording", t.Group.ID)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	text, _, withheld, err := r.Pages.Selection(ctx, t, 1000)
	if err != nil {
		return nil, err
	}
	if withheld {
		return nil, protowire.Errorf(protowire.CodeForbidden, "the selection is in a password field; select words of the page instead")
	}
	st := Step{Action: "expect"}
	if text = words(text, maxText); text != "" {
		st.Text = text
	} else {
		head, err := r.Pages.Headline(ctx, t)
		if err != nil {
			return nil, err
		}
		st.Title = words(head, maxTitle)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped {
		return nil, protowire.Errorf(protowire.CodeNotFound, "group %q is not recording", t.Group.ID)
	}
	r.commit(ctx, s, false)
	r.add(s, t, st)
	out := s.wf.Steps[len(s.wf.Steps)-1]
	return &out, nil
}

// Dialog records the person's answer to a page's dialog, before it is given.
func (r *Recorder) Dialog(t *browser.Tab, accept bool) {
	s := r.session(t.Group.ID)
	d := t.Dialog()
	if s == nil || d == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	a := accept
	r.add(s, t, Step{Action: "dialog", Kind: d.Type, Text: words(d.Message, maxText), Accept: &a})
}

// Run follows the daemon's events until stop is closed: the page a tab arrives at and the files that
// download are steps too, and a recording stops when the browser is no longer the person's.
func (r *Recorder) Run(stop <-chan struct{}) {
	cursor := r.Events.Last()
	for {
		wake := r.Events.Wait()
		evs, _ := r.Events.After(cursor, 256)
		for _, e := range evs {
			cursor = e.Seq
			r.route(e)
		}
		if len(evs) > 0 {
			continue
		}
		select {
		case <-wake:
		case <-stop:
			return
		}
	}
}

func (r *Recorder) route(e events.Event) {
	data, _ := e.Data.(map[string]any)
	if data == nil {
		return
	}
	if e.Type == "browser.exited" {
		groups, _ := data["groups"].([]string)
		for _, id := range groups {
			if r.session(id) != nil {
				_, _ = r.Stop(id, "closed")
			}
		}
		return
	}
	gid, _ := data["group_id"].(string)
	s := r.session(gid)
	if s == nil {
		return
	}
	switch e.Type {
	case "control":
		if owner, _ := data["owner"].(string); owner != browser.OwnerHuman {
			_, _ = r.Stop(gid, "control")
		}
	case "group.closed":
		_, _ = r.Stop(gid, "closed")
	case "tab.updated":
		if loading, _ := data["loading"].(bool); loading {
			return
		}
		tabID, _ := data["tab_id"].(string)
		raw, _ := data["url"].(string)
		title, _ := data["title"].(string)
		u := CleanURL(raw)
		if u == "" || u == "about:blank" {
			return
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.arrived[tabID] == u {
			// The title a page sets once it has loaded, for the arrival just recorded.
			if n := len(s.wf.Steps); n > 0 && title != "" {
				last := &s.wf.Steps[n-1]
				if last.Action == "arrive" && last.Tab == tabID && last.To == u && last.Title != words(title, maxTitle) {
					last.Title = words(title, maxTitle)
					r.stepped(s, *last, n)
				}
			}
			return
		}
		s.arrived[tabID] = u
		st := Step{Action: "arrive", To: u, Title: words(title, maxTitle), Tab: tabID, URL: u}
		r.add(s, nil, st)
	case "download.started":
		name, tab := "", ""
		if d, ok := data["download"].(page.Download); ok {
			name, tab = d.Name, d.TabID
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		st := Step{Action: "download", Text: words(name, maxTitle), Tab: tab}
		if g, err := r.Groups(gid); err == nil {
			for _, x := range g.Tabs() {
				if x.ID == tab {
					st.URL = CleanURL(x.URL())
				}
			}
		}
		r.add(s, nil, st)
	}
}
