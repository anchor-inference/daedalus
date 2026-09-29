package page

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/sensitive"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// ActParams are page.act's.
type ActParams struct {
	TabID     string          `json:"tab_id"`
	Action    string          `json:"action"`
	Ref       string          `json:"ref,omitempty"`
	ToRef     string          `json:"to_ref,omitempty"`
	Element   string          `json:"element"`
	Text      *string         `json:"text,omitempty"`
	Keys      string          `json:"keys,omitempty"`
	Option    string          `json:"option,omitempty"`
	Submit    bool            `json:"submit,omitempty"`
	Direction string          `json:"direction,omitempty"`
	UploadIDs []string        `json:"upload_ids,omitempty"`
	DryRun    bool            `json:"dry_run,omitempty"`
	Origin    *browser.Origin `json:"origin,omitempty"`
	// X and Y are a point of the viewport, in CSS pixels, to act at instead of a ref: for what the
	// outline does not name (a canvas, a map). Refused unless AllowPoint, which the host sets from
	// the operator's setting for the session; the element found there is classified and hit-tested
	// as a ref's would be.
	X          *float64 `json:"x,omitempty"`
	Y          *float64 `json:"y,omitempty"`
	AllowPoint bool     `json:"allow_point,omitempty"`
}

// MaxTypeText bounds what one type action inserts.
const MaxTypeText = 10000

// needsRef lists the actions that act on an element.
var needsRef = map[string]bool{"click": true, "double_click": true, "right_click": true, "hover": true, "type": true,
	"select": true, "check": true, "uncheck": true, "drag": true, "upload": true}

type box struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
	W float64 `json:"w"`
	H float64 `json:"h"`
}

type point struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
}

type described struct {
	Role         string `json:"role"`
	Name         string `json:"name"`
	Tag          string `json:"tag"`
	Type         string `json:"type,omitempty"`
	Autocomplete string `json:"autocomplete,omitempty"`
	Href         string `json:"href,omitempty"`
	FormAction   string `json:"form_action,omitempty"`
	Secret       bool   `json:"secret"`
	SecretKind   string `json:"secret_kind"`
	Disabled     bool   `json:"disabled"`
	Checked      bool   `json:"checked"`
	File         bool   `json:"file"`
	Select       bool   `json:"select"`
}

type prepared struct {
	Box      box       `json:"box"`
	Element  described `json:"element"`
	Viewport struct {
		W float64 `json:"w"`
		H float64 `json:"h"`
	} `json:"viewport"`
}

// ActResult is page.act's reply.
type ActResult struct {
	ActionID  string            `json:"action_id"`
	OK        bool              `json:"ok"`
	Effects   map[string]any    `json:"effects"`
	Point     *point            `json:"point,omitempty"`
	Box       *box              `json:"box,omitempty"`
	Diff      string            `json:"diff,omitempty"`
	Sensitive *sensitive.Result `json:"sensitive,omitempty"`
	Element   *described        `json:"element,omitempty"`
	// Ref is the element acted on, when the action named a point or the focus rather than a ref.
	Ref string `json:"ref,omitempty"`
}

func asWire(err error, we **wire.Error) bool { return errors.As(err, we) }

// prepare brings the element into view and measures it, in the tab's viewport.
func (p *Model) prepare(ctx context.Context, t *browser.Tab, d *doc, ref string) (*prepared, error) {
	return p.measureIn(ctx, t, d, ref, "prepare")
}

// measureIn runs prepare or measure (the same without scrolling) in the element's document, and
// moves its box into the tab's viewport.
func (p *Model) measureIn(ctx context.Context, t *browser.Tab, d *doc, ref, fn string) (*prepared, error) {
	raw, err := p.callIn(ctx, t, d, fn, ref)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var pr prepared
	if err := json.Unmarshal(raw, &pr); err != nil {
		return nil, err
	}
	if d.parent != nil {
		// Scrolling inside a frame can scroll the page around it too.
		if err := p.reoffset(ctx, t, d); err != nil {
			return nil, err
		}
		pr.Box.X += d.off.X
		pr.Box.Y += d.off.Y
	}
	return &pr, nil
}

// aim is the point inside a box an action presses: the centre, moved by an offset the action id
// decides, so repeated clicks do not land on the one pixel a page might watch, and never outside.
func aim(b box, actionID string) point {
	h := sha256.Sum256([]byte(actionID))
	u := float64(binary.BigEndian.Uint16(h[0:2]))/65535 - 0.5
	v := float64(binary.BigEndian.Uint16(h[2:4]))/65535 - 0.5
	dx := u * minf(b.W*0.3, 8)
	dy := v * minf(b.H*0.3, 4)
	return point{X: b.X + b.W/2 + dx, Y: b.Y + b.H/2 + dy}
}

func minf(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func fieldForbidden(ref, kind string) *wire.Error {
	if kind == "" {
		kind = "password"
	}
	e := wire.Errorf(browser.CodeFieldForbidden,
		"This is a password, code or payment field. Call BrowserHandoff(reason='login') and let the operator type it.")
	e.Data = map[string]any{"ref": ref, "field": kind}
	return e
}

// Act performs one action: it finds the element, refuses a secret field, classifies what the action
// would do, and, unless it is a dry run, dispatches it as real input and reports what changed.
func (p *Model) Act(ctx context.Context, t *browser.Tab, ap ActParams) (*ActResult, error) {
	if err := p.checkAct(ap); err != nil {
		return nil, err
	}
	p.mu.Lock()
	p.actions++
	id := "a" + p.run + "-" + strconv.Itoa(p.actions)
	p.mu.Unlock()
	res := &ActResult{ActionID: id, Effects: map[string]any{}}

	// The element and its document, brought into view — or, for a point, found where it is.
	var pr *prepared
	d, ref := top, ap.Ref
	pointed := ap.X != nil
	var err error
	switch {
	case pointed:
		vp := t.Group.ViewportNow()
		at := point{X: *ap.X, Y: *ap.Y}
		if at.X < 0 || at.Y < 0 || at.X >= float64(vp.W) || at.Y >= float64(vp.H) {
			return nil, wire.Errorf(wire.CodeInvalidParams, "x and y must be inside the viewport, %dx%d", vp.W, vp.H)
		}
		if d, ref, err = p.atPoint(ctx, t, at); err != nil {
			return nil, err
		}
		if pr, err = p.measureIn(ctx, t, d, ref, "measure"); err != nil {
			return nil, err
		}
		res.Ref = ref
		b := pr.Box
		res.Box, res.Element, res.Point = &b, &pr.Element, &at
	case ap.Action == "press" && ref == "":
		if d, ref, err = p.focused(ctx, t); err != nil {
			return nil, err
		}
		res.Ref = ref
	case ref != "":
		local := ""
		if d, local, err = p.docOf(ctx, t, ref); err != nil {
			return nil, err
		}
		if local == "" && (ap.Action != "scroll" || ap.Direction != "") {
			// The frame's own ref: bringing it into view (docOf did) is all a frame takes.
			return nil, wire.Errorf(wire.CodeInvalidParams, "%s is a frame; act on an element inside it", ref)
		}
		ref = local
	}
	if ref != "" && !pointed {
		fn := "prepare"
		if ap.Action == "scroll" && ap.Direction != "" {
			fn = "measure" // the pane is scrolled where it is, not first brought to the middle
		}
		if pr, err = p.measureIn(ctx, t, d, ref, fn); err != nil {
			return nil, err
		}
		b := pr.Box
		res.Box, res.Element = &b, &pr.Element
		if ap.Action != "scroll" && ap.Action != "press" && (b.W < 1 || b.H < 1) {
			return nil, wire.Errorf(wire.CodeForbidden, "%s has no size on the page; it cannot be acted on", ap.Ref)
		}
		pt := aim(b, id)
		res.Point = &pt
	}

	// Secret fields are the operator's.
	if pr != nil && pr.Element.Secret {
		chordPrintable := false
		if ap.Action == "press" {
			if c, err := parseKeys(ap.Keys); err == nil {
				chordPrintable = c.printable()
			}
		}
		if ap.Action == "type" || ap.Action == "select" || chordPrintable {
			p.needsYou(t, "field_forbidden", "The agent reached a "+strings.ReplaceAll(orDefault(pr.Element.SecretKind, "password"), "_", " ")+
				" field ("+orDefault(pr.Element.Name, ref)+") and needs you to fill it in.", t.URL(), "")
			return nil, fieldForbidden(ref, pr.Element.SecretKind)
		}
	}

	// What the action would do, for the host's policy.
	if ref != "" {
		raw, err := p.callIn(ctx, t, d, "evidence", ref, ap.Action, ap.Submit)
		if err != nil {
			return nil, err
		}
		if err := checkScript(raw); err != nil {
			return nil, err
		}
		var ev sensitive.Evidence
		_ = json.Unmarshal(raw, &ev)
		kind := ap.Action
		if ap.Action == "press" {
			if c, err := parseKeys(ap.Keys); err == nil && c.key.Key == "Enter" && c.modBits == 0 {
				kind = "press"
			} else {
				kind = "keys"
			}
		}
		s := sensitive.Classify(kind, ev, ap.Submit)
		res.Sensitive = &s
	} else {
		res.Sensitive = &sensitive.Result{Kinds: []string{}, Evidence: map[string]any{}}
	}
	if ap.DryRun {
		res.OK = true
		return res, nil
	}

	// What the page looked like, and what the group held, before.
	var before []string
	if ref != "" {
		before = p.region(ctx, t)
	}
	loaderBefore, urlBefore := t.Loader(), t.URL()
	tabsBefore := map[string]bool{}
	for _, x := range t.Group.Tabs() {
		tabsBefore[x.ID] = true
	}
	downloadsBefore := p.downloadCount(t.Group.ID)
	actor := browser.ActorAgent
	if !ap.Origin.IsAgent() {
		actor = browser.ActorOperator
	}
	event := map[string]any{"action_id": id, "group_id": t.Group.ID, "tab_id": t.ID, "actor": actor, "kind": ap.Action,
		"name": "", "element": ap.Element, "at": time.Now().UnixMilli()}
	if res.Point != nil && pr != nil {
		event["point"], event["box"] = res.Point, res.Box
		event["name"] = pr.Element.Name
	}
	if ap.Text != nil && ap.Action == "type" {
		event["text_len"] = len([]rune(*ap.Text))
	}
	if ap.Keys != "" {
		event["keys"] = ap.Keys
	}
	p.m.Publish("action", event)

	err = p.dispatch(ctx, t, ap, d, ref, pr, res, id)
	done := map[string]any{"action_id": id, "group_id": t.Group.ID, "tab_id": t.ID, "ok": err == nil}
	if err != nil {
		done["error"] = err.Error()
		done["effects"] = map[string]any{}
		p.m.Publish("action_done", done)
		return nil, err
	}

	p.settle(ctx, t, loaderBefore)
	if t.Loader() != loaderBefore || t.URL() != urlBefore {
		res.Effects["navigated"] = true
		res.Effects["url"] = t.URL()
	}
	for _, x := range t.Group.Tabs() {
		if !tabsBefore[x.ID] {
			res.Effects["new_tab"] = x.ID
		}
	}
	if dl := t.Dialog(); dl != nil {
		res.Effects["dialog"] = dl
		// The page cannot be read while the dialog is open; what the action and the answer change
		// together is told when the dialog is answered, against the page as it was before.
		if ref != "" {
			t.SetValue(dialogBeforeKey{}, &dialogBefore{loader: loaderBefore, lines: before})
		}
	}
	if dl := p.latestDownload(t.Group.ID, downloadsBefore); dl != nil {
		res.Effects["download"] = map[string]any{"id": dl.ID, "name": dl.Name, "state": dl.State}
	}
	if ref != "" && res.Effects["navigated"] == nil && t.Dialog() == nil {
		res.Diff = diff(before, p.region(ctx, t), 2000)
	}
	res.OK = true
	done["effects"] = res.Effects
	p.m.Publish("action_done", done)
	if p.AfterAction != nil {
		p.AfterAction(t, id)
	}
	return res, nil
}

// dialogBeforeKey keeps, among a tab's values, the outline from before the action that opened the
// dialog now open.
type dialogBeforeKey struct{}

type dialogBefore struct {
	loader string
	lines  []string
}

// atPoint is the element a click at pt would act on, and its document: into frames of other sites
// as the point leads.
func (p *Model) atPoint(ctx context.Context, t *browser.Tab, pt point) (*doc, string, error) {
	d := top
	x, y := pt.X, pt.Y
	for depth := 0; ; depth++ {
		raw, err := p.callIn(ctx, t, d, "elementAt", x, y)
		if err != nil {
			return nil, "", err
		}
		if err := checkScript(raw); err != nil {
			return nil, "", err
		}
		var r struct {
			Ref   string  `json:"ref"`
			Frame string  `json:"frame"`
			X     float64 `json:"x"`
			Y     float64 `json:"y"`
		}
		if err := json.Unmarshal(raw, &r); err != nil {
			return nil, "", err
		}
		if r.Frame == "" || depth >= maxFrameDepth {
			if r.Ref == "" {
				return nil, "", wire.Errorf(wire.CodeNotFound, "nothing that can be acted on is at that point")
			}
			return d, r.Ref, nil
		}
		if d, err = p.child(ctx, t, d, r.Frame, false); err != nil {
			return nil, "", err
		}
		x, y = r.X, r.Y
	}
}

// focused is the element with the focus and its document, through frames of other sites.
func (p *Model) focused(ctx context.Context, t *browser.Tab) (*doc, string, error) {
	d := top
	for depth := 0; ; depth++ {
		raw, err := p.callIn(ctx, t, d, "focusedRef")
		if err != nil {
			return nil, "", err
		}
		var r struct {
			Ref   string `json:"ref"`
			Frame string `json:"frame"`
		}
		_ = json.Unmarshal(raw, &r)
		if r.Frame == "" || depth >= maxFrameDepth {
			return d, r.Ref, nil
		}
		if d, err = p.child(ctx, t, d, r.Frame, false); err != nil {
			return nil, "", err
		}
	}
}

// region is the page's outline for an action's difference, the frames of other sites spliced in.
func (p *Model) region(ctx context.Context, t *browser.Tab) []string {
	return p.regionIn(ctx, t, top, 0)
}

func (p *Model) regionIn(ctx context.Context, t *browser.Tab, d *doc, depth int) []string {
	raw, err := p.callIn(ctx, t, d, "region")
	if err != nil {
		return nil
	}
	var r struct {
		Lines  []string `json:"lines"`
		Frames []struct {
			Ref   string `json:"ref"`
			Line  int    `json:"line"`
			Depth int    `json:"depth"`
		} `json:"frames"`
	}
	if json.Unmarshal(raw, &r) != nil || len(r.Frames) == 0 || depth >= maxFrameDepth {
		return r.Lines
	}
	under := map[int][]string{}
	for i, f := range r.Frames {
		if i >= maxCrossFrames {
			break
		}
		c, err := p.child(ctx, t, d, f.Ref, false)
		if err != nil {
			continue
		}
		under[f.Line] = indent(p.regionIn(ctx, t, c, depth+1), f.Depth+1)
	}
	out := make([]string, 0, len(r.Lines))
	for i, l := range r.Lines {
		out = append(out, l)
		out = append(out, under[i]...)
	}
	return out
}

func orDefault(s, d string) string {
	if s == "" {
		return d
	}
	return s
}

func (p *Model) checkAct(ap ActParams) error {
	if strings.TrimSpace(ap.Element) == "" || len(ap.Element) > 500 {
		return wire.Errorf(wire.CodeInvalidParams, "element must describe what is acted on, in at most 500 bytes")
	}
	switch ap.Action {
	case "click", "double_click", "right_click", "hover", "check", "uncheck":
	case "type":
		if ap.Text == nil || len([]rune(*ap.Text)) > MaxTypeText {
			return wire.Errorf(wire.CodeInvalidParams, "type needs text, at most %d characters", MaxTypeText)
		}
	case "press":
		if _, err := parseKeys(ap.Keys); err != nil {
			return wire.Errorf(wire.CodeInvalidParams, "%v", err)
		}
	case "select":
		if ap.Option == "" {
			return wire.Errorf(wire.CodeInvalidParams, "select needs the option's label")
		}
	case "scroll":
		switch ap.Direction {
		case "", "up", "down", "left", "right":
		default:
			return wire.Errorf(wire.CodeInvalidParams, "direction is up, down, left or right")
		}
		hasText := ap.Text != nil && strings.TrimSpace(*ap.Text) != ""
		if ap.Ref == "" && ap.Direction == "" && !hasText {
			return wire.Errorf(wire.CodeInvalidParams, "scroll needs a ref, a direction (up, down, left, right) or a text to find")
		}
		if hasText && (ap.Ref != "" || ap.Direction != "") {
			return wire.Errorf(wire.CodeInvalidParams, "scroll to a text takes neither a ref nor a direction")
		}
	case "drag":
		if ap.ToRef == "" {
			return wire.Errorf(wire.CodeInvalidParams, "drag needs to_ref")
		}
	case "upload":
		if len(ap.UploadIDs) == 0 || len(ap.UploadIDs) > 20 {
			return wire.Errorf(wire.CodeInvalidParams, "upload needs 1-20 upload_ids from upload.put")
		}
	default:
		return wire.Errorf(wire.CodeInvalidParams, "unknown action %q", ap.Action)
	}
	if (ap.X == nil) != (ap.Y == nil) {
		return wire.Errorf(wire.CodeInvalidParams, "a point needs both x and y")
	}
	if ap.X != nil {
		if !ap.AllowPoint {
			return wire.Errorf(wire.CodeForbidden, "acting at a point is off for this session; act on a ref from a snapshot")
		}
		if ap.Ref != "" {
			return wire.Errorf(wire.CodeInvalidParams, "a point or a ref, not both")
		}
		switch ap.Action {
		case "click", "double_click", "right_click", "hover":
		default:
			return wire.Errorf(wire.CodeInvalidParams, "only click, double_click, right_click and hover act at a point")
		}
		return nil
	}
	if needsRef[ap.Action] && ap.Ref == "" {
		return wire.Errorf(wire.CodeInvalidParams, "%s needs a ref from a snapshot", ap.Action)
	}
	return nil
}

func (p *Model) input(ctx context.Context, t *browser.Tab, method string, params any) error {
	return t.Input(ctx, method, params)
}

func (p *Model) mouse(ctx context.Context, t *browser.Tab, typ string, pt point, button string, clicks, mods int) error {
	params := map[string]any{"type": typ, "x": pt.X, "y": pt.Y, "modifiers": mods}
	if typ != "mouseMoved" {
		params["button"] = button
		params["clickCount"] = clicks
		if typ == "mousePressed" {
			params["buttons"] = map[string]int{"left": 1, "right": 2, "middle": 4}[button]
		}
	}
	return p.input(ctx, t, "Input.dispatchMouseEvent", params)
}

// reach moves the mouse to the point and, for an element in a frame of another site, waits until
// the frame's page sees the pointer over it. Right after a screenshot (the host's thumbnail before
// it asks, the recording's keyframe after an action) Chromium routes input at such a frame to the
// page around it for a moment: the press would focus the frame and do nothing in it (measured on
// the pinned build: 12 clicks of 12 lost after a screenshot, none after a second's pause). The
// tab's own elements are reached at once and not asked about.
func (p *Model) reach(ctx context.Context, t *browser.Tab, d *doc, ref string, pt point) error {
	for attempt := 0; ; attempt++ {
		if err := p.mouse(ctx, t, "mouseMoved", pt, "", 0, 0); err != nil {
			return err
		}
		if d.parent == nil || ref == "" || attempt >= 20 {
			return nil
		}
		raw, err := p.callIn(ctx, t, d, "hovered", ref)
		if err != nil {
			return err
		}
		var h struct {
			Hovered bool `json:"hovered"`
		}
		if json.Unmarshal(raw, &h) == nil && h.Hovered {
			return nil
		}
		select {
		case <-time.After(50 * time.Millisecond):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// clickIn is click on an element of d, reached first as reach says.
func (p *Model) clickIn(ctx context.Context, t *browser.Tab, d *doc, ref string, pt point, button string, count int) error {
	if err := p.reach(ctx, t, d, ref, pt); err != nil {
		return err
	}
	for i := 1; i <= count && t.Dialog() == nil; i++ {
		if err := p.mouse(ctx, t, "mousePressed", pt, button, i, 0); err != nil {
			return err
		}
		if err := p.mouse(ctx, t, "mouseReleased", pt, button, i, 0); err != nil {
			return err
		}
	}
	return nil
}

// covered refuses a click whose point another element covers: clicking it would act on something
// the snapshot did not name, which is how a page steers an agent's click. When another point of the
// element is free, that point is returned to click instead. An element in a frame is checked in its
// frame, and the frame in the page around it, since a page can cover a frame as it can an element.
func (p *Model) covered(ctx context.Context, t *browser.Tab, d *doc, ref string, pt point) (point, error) {
	raw, err := p.callIn(ctx, t, d, "hit", ref, pt.X-d.off.X, pt.Y-d.off.Y)
	if err != nil {
		return pt, err
	}
	if err := checkScript(raw); err != nil {
		return pt, err
	}
	var h struct {
		OK        bool     `json:"ok"`
		CoveredBy string   `json:"covered_by"`
		X         *float64 `json:"x"`
		Y         *float64 `json:"y"`
	}
	_ = json.Unmarshal(raw, &h)
	if !h.OK {
		return pt, coveredErr(ref, h.CoveredBy)
	}
	if h.X != nil && h.Y != nil {
		pt = point{X: *h.X + d.off.X, Y: *h.Y + d.off.Y}
	}
	for c := d; c.parent != nil; c = c.parent {
		par := c.parent
		raw, err := p.callIn(ctx, t, par, "hitFrame", c.prefix, pt.X-par.off.X, pt.Y-par.off.Y)
		if err != nil {
			return pt, err
		}
		if err := checkScript(raw); err != nil {
			return pt, err
		}
		h.OK, h.CoveredBy = false, ""
		_ = json.Unmarshal(raw, &h)
		if !h.OK {
			return pt, coveredErr(ref, h.CoveredBy)
		}
	}
	return pt, nil
}

func coveredErr(ref, by string) *wire.Error {
	e := wire.Errorf(wire.CodeForbidden, "%s is covered by %s at the point it would be clicked; deal with that first", ref, orDefault(by, "something else"))
	e.Data = map[string]any{"ref": ref, "covered_by": by}
	return e
}

func (p *Model) press(ctx context.Context, t *browser.Tab, c chord) error {
	for _, m := range c.mods {
		k := modifiers[m].key
		if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "rawKeyDown", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode, "modifiers": c.modBits}); err != nil {
			return err
		}
	}
	down := map[string]any{"type": "rawKeyDown", "key": c.key.Key, "code": c.key.Code, "windowsVirtualKeyCode": c.key.KeyCode,
		"modifiers": c.modBits}
	if c.key.Text != "" {
		down["type"] = "keyDown"
		down["text"] = c.key.Text
		down["unmodifiedText"] = c.key.Text
	}
	if err := p.input(ctx, t, "Input.dispatchKeyEvent", down); err != nil {
		return err
	}
	if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": c.key.Key, "code": c.key.Code,
		"windowsVirtualKeyCode": c.key.KeyCode, "modifiers": c.modBits}); err != nil {
		return err
	}
	for i := len(c.mods) - 1; i >= 0; i-- {
		k := modifiers[c.mods[i]].key
		if err := p.input(ctx, t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode}); err != nil {
			return err
		}
	}
	return nil
}

func (p *Model) dispatch(ctx context.Context, t *browser.Tab, ap ActParams, d *doc, ref string, pr *prepared, res *ActResult, id string) error {
	pt := point{}
	if res.Point != nil {
		pt = *res.Point
	}
	disabled := pr != nil && pr.Element.Disabled
	// hit is the covered check, moving the point the action is reported at when it had to move.
	hit := func() error {
		at, err := p.covered(ctx, t, d, ref, pt)
		if err != nil {
			return err
		}
		pt = at
		res.Point = &at
		return nil
	}
	switch ap.Action {
	case "click", "double_click", "right_click":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		if err := hit(); err != nil {
			return err
		}
		switch ap.Action {
		case "click":
			return p.clickIn(ctx, t, d, ref, pt, "left", 1)
		case "double_click":
			return p.clickIn(ctx, t, d, ref, pt, "left", 2)
		}
		return p.clickIn(ctx, t, d, ref, pt, "right", 1)
	case "hover":
		return p.reach(ctx, t, d, ref, pt)
	case "check", "uncheck":
		want := ap.Action == "check"
		if pr.Element.Checked == want {
			res.Effects["unchanged"] = true
			return nil
		}
		if err := hit(); err != nil {
			return err
		}
		return p.clickIn(ctx, t, d, ref, pt, "left", 1)
	case "type":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		// Focused by a real click where the field can be clicked, so the page sees what a person's
		// typing makes it see; then its content selected and replaced.
		if err := hit(); err == nil {
			if err := p.clickIn(ctx, t, d, ref, pt, "left", 1); err != nil {
				return err
			}
		}
		if raw, err := p.callIn(ctx, t, d, "selectAll", ref); err != nil {
			return err
		} else if err := checkScript(raw); err != nil {
			return err
		}
		if *ap.Text != "" {
			if err := p.input(ctx, t, "Input.insertText", map[string]any{"text": *ap.Text}); err != nil {
				return err
			}
		} else {
			if err := p.press(ctx, t, chord{key: named["Delete"]}); err != nil {
				return err
			}
		}
		if ap.Submit {
			return p.press(ctx, t, chord{key: named["Enter"]})
		}
		return nil
	case "press":
		c, _ := parseKeys(ap.Keys)
		if ap.Ref != "" {
			if raw, err := p.callIn(ctx, t, d, "focus", ref); err != nil {
				return err
			} else if err := checkScript(raw); err != nil {
				return err
			}
		}
		return p.press(ctx, t, c)
	case "select":
		if disabled {
			return wire.Errorf(wire.CodeForbidden, "%s is disabled", ref)
		}
		raw, err := p.callIn(ctx, t, d, "selectOption", ref, ap.Option)
		if err != nil {
			return err
		}
		return checkScript(raw)
	case "scroll":
		return p.scroll(ctx, t, ap, d, ref, res)
	case "drag":
		td, tref, err := p.docOf(ctx, t, ap.ToRef)
		if err != nil {
			return err
		}
		to, err := p.prepare(ctx, t, td, tref)
		if err != nil {
			return err
		}
		// The source may have scrolled out while the target came in: measured again.
		from, err := p.prepare(ctx, t, d, ref)
		if err != nil {
			return err
		}
		a, b := aim(from.Box, id), aim(to.Box, id+"to")
		if err := p.mouse(ctx, t, "mouseMoved", a, "", 0, 0); err != nil {
			return err
		}
		if err := p.mouse(ctx, t, "mousePressed", a, "left", 1, 0); err != nil {
			return err
		}
		for i := 1; i <= 10; i++ {
			f := float64(i) / 10
			if err := t.Call(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseMoved", "x": a.X + (b.X-a.X)*f,
				"y": a.Y + (b.Y-a.Y)*f, "button": "left", "buttons": 1}, nil); err != nil {
				return err
			}
		}
		return p.mouse(ctx, t, "mouseReleased", b, "left", 1, 0)
	case "upload":
		if !pr.Element.File {
			return wire.Errorf(wire.CodeInvalidParams, "%s is not a file input", ref)
		}
		paths, err := p.uploadPaths(t.Group.ID, ap.UploadIDs)
		if err != nil {
			return err
		}
		obj, err := p.object(ctx, t, d, "element", ref)
		if err != nil {
			return err
		}
		return t.CallIn(ctx, d.session, "DOM.setFileInputFiles", map[string]any{"files": paths, "objectId": obj}, nil)
	}
	return fmt.Errorf("unknown action %q", ap.Action)
}

// scroll moves the page: an element into view (done by prepare already), the pane that scrolls an
// element by most of its height or width, the window by most of a screen, or the page to the first
// text that holds a phrase. Every scroll is the mouse wheel, as a person scrolls; a pane that does
// not move for the wheel is scrolled by the script. effects.scroll says where the page is now.
func (p *Model) scroll(ctx context.Context, t *browser.Tab, ap ActParams, d *doc, ref string, res *ActResult) error {
	state := func() *Scrolled {
		raw, err := p.call(ctx, t, "scrollState")
		if err != nil {
			return nil
		}
		var st Scrolled
		if json.Unmarshal(raw, &st) != nil {
			return nil
		}
		return &st
	}
	defer func() {
		if st := state(); st != nil {
			res.Effects["scroll"] = st
		}
	}()
	switch {
	case ap.Text != nil && strings.TrimSpace(*ap.Text) != "":
		return p.scrollToText(ctx, t, *ap.Text, res)
	case ref != "" && ap.Direction != "":
		return p.scrollPane(ctx, t, d, ref, ap.Direction, res)
	case ap.Ref != "":
		return nil // prepare already brought it into view
	}
	// The page by a screen: the window, or the pane that scrolls in its place. What moved, and by
	// how much, is said, so a wheel that moved nothing (a list that scrolls in a pane of its own
	// elsewhere) does not pass for a scroll.
	before := state()
	vp := t.Group.ViewportNow()
	c := point{X: float64(vp.W) / 2, Y: float64(vp.H) / 2}
	res.Point = &c
	dx, dy := wheelDelta(ap.Direction, float64(vp.W), float64(vp.H))
	if err := p.wheel(ctx, t, c, dx, dy); err != nil {
		return err
	}
	time.Sleep(150 * time.Millisecond)
	if after := state(); before != nil && after != nil && (ap.Direction == "up" || ap.Direction == "down") {
		atEnd := after.Below < 0.05
		if ap.Direction == "up" {
			atEnd = after.Above < 0.05
		}
		moved := 0.0
		if before.Pane == after.Pane {
			moved = after.Top - before.Top
		}
		res.Effects["scrolled"] = map[string]any{"by": moved, "at_end": atEnd, "pane": orDefault(after.Pane, "window")}
	}
	return nil
}

// wheelDelta is most of a view in the direction: 80 %, so a line cut at the edge shows again.
func wheelDelta(direction string, w, h float64) (float64, float64) {
	switch direction {
	case "up":
		return 0, -h * 0.8
	case "left":
		return -w * 0.8, 0
	case "right":
		return w * 0.8, 0
	}
	return 0, h * 0.8
}

func (p *Model) wheel(ctx context.Context, t *browser.Tab, at point, dx, dy float64) error {
	return t.Call(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseWheel", "x": at.X, "y": at.Y,
		"deltaX": dx, "deltaY": dy}, nil)
}

type paneInfo struct {
	Window bool    `json:"window"`
	Ref    string  `json:"ref"`
	Box    box     `json:"box"`
	Top    float64 `json:"top"`
	Left   float64 `json:"left"`
	Height float64 `json:"height"`
	Width  float64 `json:"width"`
	ViewH  float64 `json:"view_h"`
	ViewW  float64 `json:"view_w"`
}

func (p *Model) pane(ctx context.Context, t *browser.Tab, d *doc, ref string, horizontal bool) (*paneInfo, error) {
	raw, err := p.callIn(ctx, t, d, "scrollInfo", ref, horizontal)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var pi paneInfo
	return &pi, json.Unmarshal(raw, &pi)
}

// scrollPane scrolls the pane an element is in (a list, a sidebar, a table that scrolls of its own),
// or the window when nothing inside the page scrolls it.
func (p *Model) scrollPane(ctx context.Context, t *browser.Tab, d *doc, ref, direction string, res *ActResult) error {
	horizontal := direction == "left" || direction == "right"
	before, err := p.pane(ctx, t, d, ref, horizontal)
	if err != nil {
		return err
	}
	vp := t.Group.ViewportNow()
	at := point{X: float64(vp.W) / 2, Y: float64(vp.H) / 2}
	w, h := float64(vp.W), float64(vp.H)
	if !before.Window {
		if err := p.reoffset(ctx, t, d); err != nil {
			return err
		}
		at = point{X: d.off.X + before.Box.X + before.Box.W/2, Y: d.off.Y + before.Box.Y + before.Box.H/2}
		w, h = before.ViewW, before.ViewH
	}
	res.Point = &at
	dx, dy := wheelDelta(direction, w, h)
	if err := p.wheel(ctx, t, at, dx, dy); err != nil {
		return err
	}
	time.Sleep(150 * time.Millisecond)
	after, err := p.pane(ctx, t, d, ref, horizontal)
	if err != nil {
		return err
	}
	moved := func() float64 {
		if horizontal {
			return after.Left - before.Left
		}
		return after.Top - before.Top
	}
	if moved() == 0 && !before.Window && before.Ref != "" {
		// A pane whose page stops the wheel (or hands it to the page around it) still scrolls.
		if raw, err := p.callIn(ctx, t, d, "scrollBy", before.Ref, dx, dy); err == nil && checkScript(raw) == nil {
			if a, err := p.pane(ctx, t, d, ref, horizontal); err == nil {
				after = a
			}
		}
	}
	atEnd := after.Top+after.ViewH >= after.Height-1
	switch direction {
	case "up":
		atEnd = after.Top <= 0
	case "left":
		atEnd = after.Left <= 0
	case "right":
		atEnd = after.Left+after.ViewW >= after.Width-1
	}
	res.Effects["scrolled"] = map[string]any{"by": moved(), "at_end": atEnd, "pane": orDefault(before.Ref, "window")}
	return nil
}

// scrollToText brings the first text holding the phrase into view: the page's own first, then its
// frames of other sites.
func (p *Model) scrollToText(ctx context.Context, t *browser.Tab, text string, res *ActResult) error {
	cross, _ := p.crossDocs(ctx, t, top, maxCrossFrames)
	for _, d := range append([]*doc{top}, cross...) {
		raw, err := p.callIn(ctx, t, d, "scrollToText", text)
		if err != nil {
			continue
		}
		var r struct {
			Found bool   `json:"found"`
			Ref   string `json:"ref"`
			Box   box    `json:"box"`
		}
		if json.Unmarshal(raw, &r) != nil || !r.Found {
			continue
		}
		res.Ref = r.Ref
		if err := p.reoffset(ctx, t, d); err == nil {
			b := r.Box
			b.X += d.off.X
			b.Y += d.off.Y
			res.Box = &b
		}
		res.Effects["found"] = r.Ref
		return nil
	}
	return wire.Errorf(wire.CodeNotFound, "no visible text on the page holds %q", text)
}

// settle gives an action's consequences a moment to show: a navigation it started is waited for to
// its load (at most 10 s), anything else for a quarter of a second.
func (p *Model) settle(ctx context.Context, t *browser.Tab, loaderBefore string) {
	deadline := time.Now().Add(400 * time.Millisecond)
	for time.Now().Before(deadline) {
		if t.Loader() != loaderBefore || t.Dialog() != nil {
			break
		}
		select {
		case <-t.Wake():
		case <-time.After(time.Until(deadline)):
		case <-ctx.Done():
			return
		}
	}
	if t.Loader() != loaderBefore && t.Dialog() == nil {
		lctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		_ = t.WaitLifecycle(lctx, "", "load")
	}
}

// diff is what an action changed near its element: lines that appeared (+) and went (-), at most
// limit bytes.
func diff(before, after []string, limit int) string {
	before, after = unfocused(before), unfocused(after)
	seen := map[string]int{}
	for _, l := range before {
		seen[l]++
	}
	var out []string
	for _, l := range after {
		if seen[l] > 0 {
			seen[l]--
			continue
		}
		out = append(out, "+ "+strings.TrimLeft(l, " "))
	}
	for _, l := range before {
		if seen[l] > 0 {
			seen[l]--
			out = append(out, "- "+strings.TrimLeft(l, " "))
		}
	}
	var b strings.Builder
	for _, l := range out {
		if b.Len()+len(l)+1 > limit {
			b.WriteString("… (more changed; take a snapshot)\n")
			break
		}
		b.WriteString(l + "\n")
	}
	return strings.TrimSuffix(b.String(), "\n")
}

// unfocused drops the focus marker: where the focus went is the least of what an action changed,
// and every click moves it.
func unfocused(lines []string) []string {
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = strings.Replace(l, " [focused]", "", 1)
	}
	return out
}
