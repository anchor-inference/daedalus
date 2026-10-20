//go:build unix

package rpc_test

import (
	"context"
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

// click is one trusted click as the screen page logged it, with what the page could read of its
// window at that moment and, for the frame's, the frame's place in the page.
type click struct {
	Where   string  `json:"where"`
	Trusted bool    `json:"trusted"`
	CX      float64 `json:"cx"`
	CY      float64 `json:"cy"`
	SX      float64 `json:"sx"`
	SY      float64 `json:"sy"`
	OX      float64 `json:"ox"`
	OY      float64 `json:"oy"`
	WSX     float64 `json:"wsx"`
	WSY     float64 `json:"wsy"`
	OW      float64 `json:"ow"`
	OH      float64 `json:"oh"`
	IW      float64 `json:"iw"`
	IH      float64 `json:"ih"`
}

// eval runs an expression in a tab's page and decodes its value.
func (h *harness) eval(tab, expr string, out any) {
	h.t.Helper()
	tb, err := h.manager.Tab(tab)
	if err != nil {
		h.t.Fatal(err)
	}
	var r struct {
		Result struct {
			Value json.RawMessage `json:"value"`
		} `json:"result"`
		Exception json.RawMessage `json:"exceptionDetails"`
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := tb.Call(ctx, "Runtime.evaluate", map[string]any{"expression": expr, "returnByValue": true}, &r); err != nil {
		h.t.Fatal(err)
	}
	if err := json.Unmarshal(r.Result.Value, out); err != nil {
		h.t.Fatalf("%s: %v (%s %s)", expr, err, r.Result.Value, r.Exception)
	}
}

func (h *harness) clicks(tab string, n int) []click {
	h.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		var cs []click
		h.eval(tab, "clicks", &cs)
		if len(cs) >= n || time.Now().After(deadline) {
			return cs
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// onScreen holds a click to what a person's click on a real window reports: its place in the page,
// plus its frame's place, plus the window's place on the screen, plus the browser's own frame (the
// sides alike, the toolbar the rest of the height).
func onScreen(t *testing.T, c click) {
	t.Helper()
	if !c.Trusted {
		t.Fatalf("%s: an untrusted click: %+v", c.Where, c)
	}
	side := (c.OW - c.IW) / 2
	bar := c.OH - c.IH - side
	t.Logf("%s: window at (%v, %v), %vx%v around a %vx%v page", c.Where, c.WSX, c.WSY, c.OW, c.OH, c.IW, c.IH)
	wantX := c.CX + c.OX + c.WSX + side
	wantY := c.CY + c.OY + c.WSY + bar
	if math.Abs(c.SX-wantX) > 1 || math.Abs(c.SY-wantY) > 1 {
		t.Fatalf("%s: screen (%v, %v), a real window gives (%v, %v): %+v", c.Where, c.SX, c.SY, wantX, wantY, c)
	}
	if c.SX == c.CX && c.SY == c.CY {
		t.Fatalf("%s: the screen position is the page position: %+v", c.Where, c)
	}
}

// The agent's clicks and the operator's land on the screen where a person's would: in the page and
// in a frame of another site, whether or not the browser needed the fix. The browsers measured here
// report it right of themselves, so with the probe left to decide no script is put in and the
// getters stay the browser's own; with the fix forced on, it is in every document, the frame of
// another site and a new tab included, and still changes nothing a correct browser reports.
func TestClicksLandOnTheScreen(t *testing.T) {
	for _, fix := range []string{config.ScreenFixAuto, config.ScreenFixAlways} {
		t.Run(fix, func(t *testing.T) {
			if fix == config.ScreenFixAlways {
				launchOnly(t, "the screen position script is never put into the operator's pages")
			}
			h := crossSitesWith(t, func(c *config.Config) { c.Input.ScreenFix = fix })
			o := h.open("g1", "project-a", "")
			tab := o.Tab.ID
			h.must("page.navigate", map[string]any{"tab_id": tab, "url": "http://shop.example/xsite/screen"}, nil)
			var s = h.snapshot(tab)
			deadline := time.Now().Add(10 * time.Second)
			for !strings.Contains(s.Text, "Frame button") {
				if time.Now().After(deadline) {
					t.Fatalf("the frame was not read:\n%s", s.Text)
				}
				time.Sleep(200 * time.Millisecond)
				s = h.snapshot(tab)
			}
			h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Top button"), "element": "top"})
			h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Frame button"), "element": "frame"})
			cs := h.clicks(tab, 2)
			if len(cs) != 2 || cs[0].Where != "b" || cs[1].Where != "frame" {
				t.Fatalf("clicks: %+v", cs)
			}
			for _, c := range cs {
				onScreen(t, c)
			}

			// The operator takes over and clicks the top button through the live view.
			v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
			v.waitEvent("hello", 10*time.Second, nil)
			h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, nil)
			for _, typ := range []string{"move", "down", "up"} {
				v.input(map[string]any{"t": "mouse", "type": typ, "x": 210, "y": 120, "button": "left", "clicks": 1})
			}
			cs = h.clicks(tab, 3)
			if len(cs) != 3 || cs[2].Where != "b" {
				t.Fatalf("the operator's click: %+v", cs)
			}
			onScreen(t, cs[2])
			h.must("control.set", map[string]any{"group_id": "g1", "owner": "agent"}, nil)

			native := "function get screenX() { [native code] }"
			var top, frame string
			h.eval(tab, `Function.prototype.toString.call(Object.getOwnPropertyDescriptor(MouseEvent.prototype, "screenX").get)`, &top)
			h.eval(tab, "frameGetter", &frame)
			var nt opened
			h.must("tab.new", map[string]any{"group_id": "g1", "url": "http://shop.example/xsite/screen"}, &nt.Tab)
			var later string
			deadline = time.Now().Add(10 * time.Second)
			for {
				h.eval(nt.Tab.ID, `typeof getter === "function" ? getter() : ""`, &later)
				if later != "" || time.Now().After(deadline) {
					break
				}
				time.Sleep(100 * time.Millisecond)
			}
			for name, got := range map[string]string{"the page": top, "the frame of another site": frame, "a new tab": later} {
				if fix == config.ScreenFixAuto && got != native {
					t.Errorf("%s: a browser that reports right got the script anyway: %q", name, got)
				}
				if fix == config.ScreenFixAlways && (got == native || got == "") {
					t.Errorf("%s: the forced fix is not there: %q", name, got)
				}
			}
		})
	}
}
