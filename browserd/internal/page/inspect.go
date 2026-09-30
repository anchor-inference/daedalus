package page

import (
	"context"
	"encoding/json"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// InspectMax is the most characters of an element's markup page.inspect returns.
const InspectMax = 50_000

// Inspect is one element as a developer's tools show it (js/page.js, inspect): its markup without
// scripts, handlers or secret values, its box in the tab's viewport, the styles that decide whether
// it shows and takes a click, its state, and why it is not seen or not clickable. It reads in the
// element's own world and moves nothing, except that an element inside a frame of another site has
// its frame brought into view first, as every call on such a ref does.
//
// selector, instead of a ref, finds the element by a CSS selector in the tab's own document: the one
// the outline does not show — hidden, or no control — is what a developer asks about, and by the
// selector of their own code. The element found is given a ref.
func (p *Model) Inspect(ctx context.Context, t *browser.Tab, ref, selector string, maxHTML int) (map[string]any, error) {
	if maxHTML == 0 {
		maxHTML = 4000
	}
	if maxHTML < 200 || maxHTML > InspectMax {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_chars is 200-%d", InspectMax)
	}
	if (ref == "") == (selector == "") {
		return nil, wire.Errorf(wire.CodeInvalidParams, "give a ref or a selector")
	}
	if selector != "" {
		if len(selector) > 500 {
			return nil, wire.Errorf(wire.CodeInvalidParams, "selector is at most 500 bytes")
		}
		raw, err := p.call(ctx, t, "inspect", "", maxHTML, selector)
		if err != nil {
			return nil, err
		}
		if err := checkScript(raw); err != nil {
			return nil, err
		}
		var out map[string]any
		if err := json.Unmarshal(raw, &out); err != nil {
			return nil, err
		}
		return out, nil
	}
	if !isRef(ref) && !isFrameRef(ref) {
		return nil, wire.Errorf(wire.CodeInvalidParams, "ref is an element's ref from the snapshot (e14, f2e4)")
	}
	d, local, err := p.docOf(ctx, t, ref)
	if err != nil {
		return nil, err
	}
	if local == "" {
		// A frame's own ref: the frame is an element of the document around it.
		d, local = d.parent, ref
		if d == nil {
			return nil, errStale(ref)
		}
	}
	raw, err := p.callIn(ctx, t, d, "inspect", local, maxHTML)
	if err != nil {
		return nil, err
	}
	if err := checkScript(raw); err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	out["ref"] = ref
	if d.parent != nil {
		// The box was measured in the frame's viewport: moved into the tab's, as an action's is.
		if err := p.reoffset(ctx, t, d); err == nil {
			if b, ok := out["box"].(map[string]any); ok {
				if x, ok := b["x"].(float64); ok {
					b["x"] = x + d.off.X
				}
				if y, ok := b["y"].(float64); ok {
					b["y"] = y + d.off.Y
				}
			}
		}
		out["frame"] = d.prefix
	}
	return out, nil
}
