package page

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/sensitive"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Field is what the recording of a person's steps knows about an element they pressed or typed
// into (js/page.js, recordInfo).
type Field struct {
	Role     string `json:"role"`
	Name     string `json:"name"`
	Tag      string `json:"tag"`
	Type     string `json:"type"`
	Place    string `json:"place"`
	Href     string `json:"href,omitempty"`
	Secret   string `json:"secret_kind"`
	Near     string `json:"near_secret"`
	Personal bool   `json:"personal"`
	Text     bool   `json:"text_field"`
	Multi    bool   `json:"multiline"`
	Check    bool   `json:"checkable"`
	Checked  bool   `json:"checked"`
	Select   bool   `json:"select"`
}

// Target is an element a person acted on, as the recording keeps it: described, classified as an
// agent's action on it would be, and with a handle to read its value again when they are done with
// it. Key is the same for the same element of the same document, so a click into a field and the
// typing that follows are known to be one step.
type Target struct {
	Key       string
	Field     Field
	Sensitive []string
	tab       *browser.Tab
	doc       *doc
	ref       string
	loader    string
}

// Value is a field's value as the recording reads it (js/page.js, recordValue). Withheld is a secret
// field, whose value never leaves the page.
type Value struct {
	Value    string `json:"value"`
	Option   string `json:"option"`
	Checked  bool   `json:"checked"`
	Withheld bool   `json:"withheld"`
}

func (p *Model) target(ctx context.Context, t *browser.Tab, d *doc, ref, action string) (*Target, error) {
	raw, err := p.callIn(ctx, t, d, "recordInfo", ref)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	tg := &Target{tab: t, doc: d, ref: ref, loader: t.Loader()}
	if err := json.Unmarshal(raw, &tg.Field); err != nil {
		return nil, fmt.Errorf("the page script described an element that is not one: %w", err)
	}
	tg.Key = t.ID + "/" + tg.loader + "/" + d.session + "/" + d.frame + "/" + ref
	// Classified as the agent's own action on it would be, so the recording knows the press that
	// signs in (credentials) from one that buys or sends.
	if action != "" {
		raw, err := p.callIn(ctx, t, d, "evidence", ref, action, false)
		if err == nil && checkScript(raw) == nil {
			var ev sensitive.Evidence
			if json.Unmarshal(raw, &ev) == nil {
				tg.Sensitive = sensitive.Classify(action, ev, false).Kinds
			}
		}
	}
	return tg, nil
}

// TargetAt is what a person's press at a point of the viewport lands on: the control the element
// there belongs to, through frames of other sites, as an agent's action at that point would find it.
func (p *Model) TargetAt(ctx context.Context, t *browser.Tab, x, y float64) (*Target, error) {
	vp := t.Group.ViewportNow()
	if x < 0 || y < 0 || x >= float64(vp.W) || y >= float64(vp.H) {
		return nil, wire.Errorf(wire.CodeInvalidParams, "the point is outside the viewport")
	}
	d, ref, err := p.atPoint(ctx, t, point{X: x, Y: y})
	if err != nil {
		return nil, err
	}
	return p.target(ctx, t, d, ref, "click")
}

// FocusedTarget is the element with the focus, through frames of other sites; nil when the focus is
// on the page itself.
func (p *Model) FocusedTarget(ctx context.Context, t *browser.Tab) (*Target, error) {
	d, ref, err := p.focused(ctx, t)
	if err != nil || ref == "" {
		return nil, err
	}
	return p.target(ctx, t, d, ref, "")
}

// TargetValue reads a target's value now. A target whose document is gone (the page moved on) is a
// stale ref.
func (p *Model) TargetValue(ctx context.Context, tg *Target) (Value, error) {
	var v Value
	if tg == nil || tg.tab == nil {
		return v, errStale("")
	}
	if tg.tab.Loader() != tg.loader || tg.tab.Closed() {
		return v, errStale(tg.ref)
	}
	raw, err := p.callIn(ctx, tg.tab, tg.doc, "recordValue", tg.ref)
	if err != nil {
		return v, err
	}
	if err := checkScript(raw); err != nil {
		return v, err
	}
	err = json.Unmarshal(raw, &v)
	return v, err
}

// Headline is what the page says it is about: its first heading, else its title.
func (p *Model) Headline(ctx context.Context, t *browser.Tab) (string, error) {
	raw, err := p.call(ctx, t, "headline")
	if err != nil {
		return "", err
	}
	var s string
	err = json.Unmarshal(raw, &s)
	return s, err
}
