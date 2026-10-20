//go:build unix

package rpc_test

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/relay"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The attach backend's own tests, over the fake extension (attach_test.go).

// attachOnly skips a test of the attach backend in the launch run.
func attachOnly(t *testing.T) {
	t.Helper()
	if !attachRun() {
		t.Skip("attach backend only: run with BROWSERD_BACKEND=attach")
	}
}

// eventOf is the newest event of type typ in the daemon's log, or nil.
func (h *harness) eventOf(typ string) map[string]any {
	evs, _ := h.evlog.After(0, 20000)
	for i := len(evs) - 1; i >= 0; i-- {
		if evs[i].Type == typ {
			m, _ := evs[i].Data.(map[string]any)
			return m
		}
	}
	return nil
}

func (h *harness) waitEventOf(typ string, within time.Duration) map[string]any {
	h.t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if e := h.eventOf(typ); e != nil {
			return e
		}
		time.Sleep(20 * time.Millisecond)
	}
	h.t.Fatalf("no %s event within %s", typ, within)
	return nil
}

// A daemon that launches its own browsers takes no relay.
func TestRelayOpenOnALaunchDaemon(t *testing.T) {
	launchOnly(t, "it is the launch daemon's refusal")
	h := start(t, nil)
	err := h.call("relay.open", map[string]any{"transport": "native"}, nil)
	if code(err) != protowire.CodeUnsupported {
		t.Fatalf("relay.open: %v", err)
	}
	var info struct {
		Backend string `json:"backend"`
		Relay   any    `json:"relay"`
	}
	h.must("daemon.info", map[string]any{}, &info)
	if info.Backend != "launch" || info.Relay != nil {
		t.Fatalf("daemon.info %+v", info)
	}
}

// What would change the operator's own browser beyond the agent's tabs is refused with 1004.
func TestAttachRefusals(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	for _, c := range []struct {
		method string
		params map[string]any
	}{
		{"profile.clear", map[string]any{"profile": "project-a"}},
		{"profile.delete", map[string]any{"profile": "project-a"}},
		{"cookies.set", map[string]any{"group_id": o.Group.ID, "cookies": []map[string]any{{"name": "sid", "value": "v", "domain": "example.com"}}}},
		{"cookies.clear", map[string]any{"group_id": o.Group.ID, "domain": "example.com"}},
		{"browser.open", map[string]any{"group_id": "e1", "profile": browser.EphemeralProfile}},
	} {
		if err := h.call(c.method, c.params, nil); code(err) != protowire.CodeForbidden {
			t.Errorf("%s: %v", c.method, err)
		}
	}
	var profiles struct {
		Profiles []any `json:"profiles"`
	}
	h.must("profile.list", map[string]any{}, &profiles)
	if len(profiles.Profiles) != 0 {
		t.Fatalf("profiles %v", profiles.Profiles)
	}
	// Names come back, values never: the fixture's page sets none, so a site's list is empty, and the
	// call went to the extension rather than to Storage.
	var cookies struct {
		Cookies []map[string]any `json:"cookies"`
	}
	h.must("cookies.list", map[string]any{"group_id": o.Group.ID}, &cookies)
	if cookies.Cookies == nil {
		t.Fatal("no cookie list")
	}
}

// Everything the suite did went to the fake as per-tab commands on the allowlist and calls: the
// daemon never needed the browser itself.
func TestAttachOnlyTabsAndCalls(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	o := h.open("g1", "project-a", "/button")
	s := h.snapshot(o.Tab.ID)
	h.mustAct(o.Tab.ID, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Press"), "element": "press"})
	h.must("page.screenshot", map[string]any{"tab_id": o.Tab.ID}, nil)
	h.must("tab.new", map[string]any{"group_id": "g1", "url": h.site.URL + "/still"}, nil)
	var cdps int
	for _, f := range h.fake.Seen() {
		switch f.T {
		case "cdp":
			cdps++
			if err := relay.Allowed(f.Method); err != nil {
				t.Errorf("sent %s: %v", f.Method, err)
			}
			if f.Method == "Emulation.setUserAgentOverride" || f.Method == "Page.addScriptToEvaluateOnNewDocument" {
				t.Errorf("sent %s into the operator's page", f.Method)
			}
			if _, _, ok := relay.ParseSession(f.Session); !ok {
				t.Errorf("a command to %q", f.Session)
			}
		case "call":
			switch f.Op {
			case relay.OpWindowEnsure, relay.OpTabCreate, relay.OpWindowBounds, relay.OpTabActivate:
			default:
				t.Errorf("a call of %s", f.Op)
			}
		}
	}
	if cdps < 20 {
		t.Fatalf("only %d commands", cdps)
	}
	var info struct {
		Backend  string `json:"backend"`
		Chromium struct {
			Kind    string `json:"kind"`
			Version string `json:"version"`
		} `json:"chromium"`
		Relay struct {
			Connected   bool   `json:"connected"`
			ProfileName string `json:"profile_name"`
			Transport   string `json:"transport"`
			ExtVersion  string `json:"ext_version"`
		} `json:"relay"`
	}
	h.must("daemon.info", map[string]any{}, &info)
	if info.Backend != "attach" || info.Chromium.Kind != "relay" || info.Chromium.Version == "" || !info.Relay.Connected ||
		info.Relay.ProfileName != "Fake Chrome" || info.Relay.Transport != "native" || info.Relay.ExtVersion != "0.0.0-test" {
		t.Fatalf("daemon.info %+v", info)
	}
}

// The connection check on both sides: the popup's check is confirmed with host_seen once the host
// listens and published for the app; the app's check reaches the extension and comes back timed.
func TestAttachConnectionCheck(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	ok, err := h.fake.Check(1, 5*time.Second)
	if err != nil || ok.HostSeen {
		t.Fatalf("a check with nobody listening: %+v %v", ok, err)
	}
	if err := h.client.Call(t.Context(), "events.subscribe", map[string]any{"after_seq": 0}, nil); err != nil {
		t.Fatal(err)
	}
	ok, err = h.fake.Check(2, 5*time.Second)
	if err != nil || !ok.HostSeen {
		t.Fatalf("a check with the host listening: %+v %v", ok, err)
	}
	e, err := h.client.WaitEvent("relay.check", "", 5*time.Second)
	if err != nil || !strings.Contains(string(e.Data), "Fake Chrome") {
		t.Fatalf("relay.check: %s %v", e.Data, err)
	}
	var r struct {
		RTTMs *int64 `json:"rtt_ms"`
	}
	h.must("relay.notify", map[string]any{"text": "checked"}, &r)
	if r.RTTMs == nil {
		t.Fatal("no round trip")
	}
	var notified bool
	for _, f := range h.fake.Seen() {
		if f.T == "call" && f.Op == relay.OpNotify && strings.Contains(string(f.Args), "checked") {
			notified = true
		}
	}
	if !notified {
		t.Fatal("the extension was not told")
	}
	var st struct {
		Connected bool `json:"connected"`
	}
	h.must("relay.status", map[string]any{}, &st)
	if !st.Connected {
		t.Fatal("relay.status says not connected")
	}
}

// One relay at a time, over the real socket: a second Chrome is refused with the first one's name.
func TestAttachSecondRelayRefused(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	var r struct {
		Channel uint32 `json:"channel"`
	}
	h.must("relay.open", map[string]any{"transport": "server"}, &r)
	hello, _ := json.Marshal(relay.Hello{T: relay.THello, Proto: relay.Proto, ProfileID: "other", ProfileName: "Laptop"})
	if err := h.client.Send(r.Channel, append([]byte{relay.PieceLast}, hello...)); err != nil {
		t.Fatal(err)
	}
	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-h.client.Frames:
			if f.Channel != r.Channel || len(f.Payload) == 0 {
				continue
			}
			var bye relay.Bye
			_ = json.Unmarshal(f.Payload[1:], &bye)
			if bye.Reason != relay.ByeAnotherRelay || !strings.Contains(bye.Message, "Fake Chrome") {
				t.Fatalf("bye %+v", bye)
			}
			return
		case <-deadline:
			t.Fatal("no bye")
		}
	}
}

// The relay lost, the browser is gone and its groups say so; connected again, the group opens on the
// tabs its Chrome tab group kept.
func TestAttachRelayLostAndBack(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	h.fake.Disconnect()
	lost := h.waitEventOf("relay.lost", 5*time.Second)
	if lost["reason"] != "closed" {
		t.Fatalf("relay.lost %v", lost)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		err := h.call("tab.list", map[string]any{"group_id": o.Group.ID}, nil)
		if code(err) == browser.CodeBrowserGone {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the group after the relay was lost: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	var st struct {
		Connected bool   `json:"connected"`
		LastError string `json:"last_error"`
	}
	h.must("relay.status", map[string]any{}, &st)
	if st.Connected || st.LastError == "" {
		t.Fatalf("relay.status %+v", st)
	}
	if err := h.call("browser.open", map[string]any{"group_id": "g2", "profile": "project-a"}, nil); code(err) != protowire.CodeUnsupported {
		t.Fatalf("an open with no Chrome: %v", err)
	}
	if err := h.fake.Connect(); err != nil {
		t.Fatal(err)
	}
	again := h.open("g1", "project-a", "")
	if !again.Created || !strings.HasSuffix(again.Tab.URL, "/still") {
		t.Fatalf("the group did not take up its tab: %+v", again)
	}
	var list struct {
		Tabs []any `json:"tabs"`
	}
	h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
	if len(list.Tabs) != 1 {
		t.Fatalf("%d tabs", len(list.Tabs))
	}
}

// A popup of an agent tab joins its group through the extension's tabs frame, and a tab the operator
// opens on their own is never the daemon's.
func TestAttachPopupsAndTheOperatorsTabs(t *testing.T) {
	attachOnly(t)
	h := start(t, nil)
	o := h.open("g1", "project-a", "/popup")
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	var own struct {
		TargetID string `json:"targetId"`
	}
	if err := h.fake.Conn().Call(ctx, "", "Target.createTarget", map[string]any{"url": h.site.URL + "/still", "newWindow": true}, &own); err != nil {
		t.Fatal(err)
	}
	s := h.snapshot(o.Tab.ID)
	h.mustAct(o.Tab.ID, map[string]any{"action": "click", "ref": refOf(t, s, "link", "open"), "element": "open"})
	var list struct {
		Tabs []struct {
			URL    string `json:"url"`
			Opener any    `json:"opener"`
		} `json:"tabs"`
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
		if len(list.Tabs) == 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if len(list.Tabs) != 2 || list.Tabs[1].Opener != o.Tab.ID {
		t.Fatalf("tabs %+v", list.Tabs)
	}
	var targets struct {
		TargetInfos []struct {
			TargetID string `json:"targetId"`
		} `json:"targetInfos"`
	}
	if err := h.fake.Conn().Call(ctx, "", "Target.getTargets", nil, &targets); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, ti := range targets.TargetInfos {
		found = found || ti.TargetID == own.TargetID
	}
	if !found {
		t.Fatal("the operator's own tab was closed")
	}
	if h.fake.TabOf(own.TargetID) != 0 {
		t.Fatal("the operator's own tab became a debuggee")
	}
}
