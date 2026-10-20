package rpc_test

import (
	"bytes"
	"encoding/base64"
	"regexp"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

type recFrame struct {
	No       int64  `json:"no"`
	Kind     string `json:"kind"`
	ActionID string `json:"action_id"`
	W        int    `json:"w"`
	VW       int    `json:"vw"`
}

func (h *harness) frames(group string) []recFrame {
	var r struct {
		Frames []recFrame `json:"frames"`
	}
	h.must("record.list", map[string]any{"group_id": group}, &r)
	return r.Frames
}

func (h *harness) waitFrames(group string, n int, within time.Duration) []recFrame {
	h.t.Helper()
	deadline := time.Now().Add(within)
	for {
		got := h.frames(group)
		if len(got) >= n {
			return got
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("%d keyframes after %s, wanted %d: %+v", len(got), within, n, got)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

var buttonRef = regexp.MustCompile(`button "Press" \[ref=(e\d+)\]`)

// Recording: a frame when it starts and one after every action, carrying the action's id; a page
// that changes while a person drives is not recorded unless they chose to be; switching it off
// keeps what was taken.
func TestRecordingKeepsKeyframesOfActions(t *testing.T) {
	needChromium(t)
	h := start(t, nil)
	o := h.open("g1", "project-a", "/button")
	var st struct {
		Frames bool `json:"frames"`
	}
	h.must("record.set", map[string]any{"group_id": "g1", "frames": true}, &st)
	if !st.Frames {
		t.Fatal("recording did not switch on")
	}
	first := h.waitFrames("g1", 1, 5*time.Second)
	if first[0].Kind != "start" || first[0].W != 1280 || first[0].VW != 1280 {
		t.Fatalf("the first frame: %+v", first[0])
	}
	var snap struct {
		Text string `json:"text"`
	}
	h.must("page.snapshot", map[string]any{"tab_id": o.Tab.ID}, &snap)
	m := buttonRef.FindStringSubmatch(snap.Text)
	if m == nil {
		t.Fatalf("no button in %q", snap.Text)
	}
	var act struct {
		ActionID string `json:"action_id"`
	}
	h.must("page.act", map[string]any{"tab_id": o.Tab.ID, "action": "click", "ref": m[1], "element": "the Press button"}, &act)
	got := h.waitFrames("g1", 2, 5*time.Second)
	if got[1].Kind != "action" || got[1].ActionID != act.ActionID {
		t.Fatalf("the action's frame: %+v (action %s)", got[1], act.ActionID)
	}
	var read struct {
		Data string `json:"data_b64"`
	}
	h.must("record.read", map[string]any{"group_id": "g1", "no": got[1].No}, &read)
	jpeg, _ := base64.StdEncoding.DecodeString(read.Data)
	if !bytes.HasPrefix(jpeg, []byte{0xff, 0xd8}) {
		t.Fatal("the frame is not a JPEG")
	}
	if err := h.call("record.read", map[string]any{"group_id": "g1", "no": 999}, nil); code(err) != 1001 {
		t.Fatalf("a frame that is not kept: %v", err)
	}

	// A person takes the browser: the page changes under their hand, and nothing is recorded.
	v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	v.waitEvent("hello", 10*time.Second, nil)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, nil)
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/anim", "origin": map[string]any{"actor": "operator"}}, nil)
	time.Sleep(6 * time.Second)
	if n := len(h.frames("g1")); n != 2 {
		t.Fatalf("%d frames while a person drove; recording must pause", n)
	}
	// Unless they chose to be recorded.
	h.must("record.set", map[string]any{"group_id": "g1", "frames": true, "human": true}, nil)
	h.waitFrames("g1", 3, 8*time.Second)

	h.must("record.set", map[string]any{"group_id": "g1", "frames": false}, nil)
	var groups struct {
		Groups []struct {
			GroupID string `json:"group_id"`
			Frames  int    `json:"frames"`
		} `json:"groups"`
		Bytes int64 `json:"bytes"`
	}
	h.must("record.groups", nil, &groups)
	if len(groups.Groups) != 1 || groups.Groups[0].Frames < 3 || groups.Bytes <= 0 {
		t.Fatalf("switched off, the frames must stay: %+v", groups)
	}
	h.must("record.delete", map[string]any{"group_id": "g1"}, nil)
	if n := len(h.frames("g1")); n != 0 {
		t.Fatalf("%d frames after delete", n)
	}
	if err := h.call("limits.set", map[string]any{"max_browsers": 0}, nil); code(err) != -32602 {
		t.Fatalf("a cap of 0: %v", err)
	}
	var lim struct {
		MaxBrowsers int   `json:"max_browsers"`
		IdleCloseMs int64 `json:"idle_close_ms"`
	}
	h.must("limits.set", map[string]any{"max_browsers": 3, "idle_close_ms": 60000}, &lim)
	if lim.MaxBrowsers != 3 || lim.IdleCloseMs != 60000 {
		t.Fatalf("limits: %+v", lim)
	}
}

// daemon.info names the Chromium's version before any browser ran, and knows the sandbox's state
// within a few seconds of the first browser starting, not at the first ten-second statistics.
func TestDaemonInfoKnowsTheBrowserEarly(t *testing.T) {
	launchOnly(t, "the sandbox reported is of a Chromium the daemon runs")
	needChromium(t)
	h := start(t, nil)
	var info struct {
		Chromium struct {
			Version string `json:"version"`
		} `json:"chromium"`
		Capabilities struct {
			Sandbox string `json:"sandbox"`
		} `json:"capabilities"`
	}
	deadline := time.Now().Add(10 * time.Second)
	for h.must("daemon.info", nil, &info); info.Chromium.Version == ""; h.must("daemon.info", nil, &info) {
		if time.Now().After(deadline) {
			t.Fatal("no Chromium version before a browser ran")
		}
		time.Sleep(100 * time.Millisecond)
	}
	h.open("g1", "project-a", "/still")
	deadline = time.Now().Add(5 * time.Second)
	for h.must("daemon.info", nil, &info); info.Capabilities.Sandbox == "unknown"; h.must("daemon.info", nil, &info) {
		if time.Now().After(deadline) {
			t.Fatal("the sandbox is still unknown five seconds after a page loaded")
		}
		time.Sleep(100 * time.Millisecond)
	}
	if info.Capabilities.Sandbox != "ok" {
		t.Fatalf("sandbox: %s", info.Capabilities.Sandbox)
	}
}

// A view whose page is out of sight is sent nothing, and gets the newest picture when it is back.
func TestAHiddenViewIsSentNoFrames(t *testing.T) {
	needChromium(t)
	h := start(t, nil)
	h.open("g1", "project-a", "/anim")
	v := h.attach("g1", wire.Attach{Tier: "live", MaxW: 640, MaxH: 400})
	f, ok := v.frame(10 * time.Second)
	if !ok {
		t.Fatal("no first frame")
	}
	hidden := true
	b, _ := wire.EncodeJSON(wire.TypeView, wire.View{Hidden: &hidden})
	if err := h.client.Send(v.channel, b); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	v.ack(f.FrameNo)
	// Drain what was in flight before the VIEW arrived, then expect silence on an animating page.
	for {
		if next, ok := v.frame(1500 * time.Millisecond); ok {
			v.ack(next.FrameNo)
			continue
		}
		break
	}
	if _, ok := v.frame(2 * time.Second); ok {
		t.Fatal("a hidden view was sent a frame")
	}
	hidden = false
	b, _ = wire.EncodeJSON(wire.TypeView, wire.View{Hidden: &hidden})
	if err := h.client.Send(v.channel, b); err != nil {
		t.Fatal(err)
	}
	if _, ok := v.frame(3 * time.Second); !ok {
		t.Fatal("a view back in sight got no frame")
	}
}
