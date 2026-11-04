//go:build unix

package rpc_test

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/page"
	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// A popup over the page is reported by every read, with the ref that closes it, and closing it by
// that ref clears it: an agent once read the words under a sign-in box for a whole turn, waiting for
// search results the box was holding back.
func TestPopupsAreReported(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/still")
	tab := o.Tab.ID
	for _, c := range []struct{ path, kind, name string }{
		{"/pop/dialog", "dialog", "Subscribe to our letter"},
		{"/pop/aria", "dialog", "Cookie settings"},
		{"/pop/fixed", "overlay", "We have moved to a new address."},
	} {
		h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + c.path}, nil)
		s := h.snapshot(tab)
		b := s.Blocker
		if b == nil || b.Kind != c.kind || !strings.Contains(b.Name, c.name) || b.Close == "" || b.Ref == "" {
			t.Fatalf("%s: blocker %+v in:\n%s", c.path, b, s.Text)
		}
		var text page.Text
		h.must("page.text", map[string]any{"tab_id": tab}, &text)
		if text.Blocker == nil || text.Blocker.Close != b.Close {
			t.Fatalf("%s: the text's blocker %+v, the snapshot's %+v", c.path, text.Blocker, b)
		}
		h.mustAct(tab, map[string]any{"action": "click", "ref": b.Close, "element": "the popup's close button"})
		if s := h.snapshot(tab); s.Blocker != nil {
			t.Fatalf("%s: still blocked after closing it: %+v\n%s", c.path, s.Blocker, s.Text)
		}
	}
	// Neither a page without a popup nor an app pinned whole to the window is one.
	for _, path := range []string{"/still", "/pop/shell"} {
		h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + path}, nil)
		if s := h.snapshot(tab); s.Blocker != nil {
			t.Fatalf("%s: a popup where there is none: %+v", path, s.Blocker)
		}
	}
}

// A click on something a popup covers is refused naming the cover by its ref and the popup with
// its close control, and a wait that times out under a popup says so.
func TestCoveredClickAndWaitNameThePopup(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/pop/fixed")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	if s.Blocker == nil {
		t.Fatalf("no popup in:\n%s", s.Text)
	}
	_, err := h.act(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Search"), "element": "search"})
	var we *protowire.Error
	if !errors.As(err, &we) || we.Code != 1004 {
		t.Fatalf("a covered click: %v", err)
	}
	data := fmt.Sprint(we.Data)
	if !strings.Contains(data, "covered_ref:") || !strings.Contains(data, "close:"+s.Blocker.Close) {
		t.Fatalf("the refusal's data: %s", data)
	}
	var w map[string]any
	h.must("page.wait", map[string]any{"tab_id": tab, "for": "text", "value": "¥", "timeout_ms": 400}, &w)
	if w["matched"] != "timeout" || w["blocker"] == nil {
		t.Fatalf("a wait under a popup: %v", w)
	}
}

// A sign-in box from another site: the frame in the popup is read with refs of its own, its close
// control is found in the frame's own world, its password field stays the operator's, and closing
// it by its ref clears the page.
func TestPopupWithASignInFrameOfAnotherSite(t *testing.T) {
	h := crossSites(t)
	o := h.open("g1", "project-a", "")
	tab := o.Tab.ID
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": "http://shop.example/xsite/login-modal"}, nil)
	var s page.Snapshot
	deadline := time.Now().Add(10 * time.Second)
	for {
		s = h.snapshot(tab)
		if strings.Contains(s.Text, "密码登录") && s.Blocker != nil && s.Blocker.Close != "" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the sign-in frame was not read or its popup not reported: %+v\n%s", s.Blocker, s.Text)
		}
		time.Sleep(200 * time.Millisecond)
	}
	b := s.Blocker
	if b.Frame == "" || !b.CrossOrigin || !strings.HasPrefix(b.Close, b.Frame+"e") || !strings.Contains(b.Name, "pay.example") {
		t.Fatalf("blocker: %+v", b)
	}
	account := refOf(t, s, "textbox", "账号")
	if !strings.HasPrefix(account, b.Frame) {
		t.Fatalf("the account field's ref %s is not the frame's %s", account, b.Frame)
	}
	h.mustAct(tab, map[string]any{"action": "type", "ref": account, "text": "someone", "element": "account"})
	if _, err := h.act(tab, map[string]any{"action": "type", "ref": refOf(t, s, "textbox", "密码"), "text": "x", "element": "password"}); code(err) != 1105 {
		t.Fatalf("typing a password: %v", err)
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": b.Close, "element": "close the sign-in box"})
	deadline = time.Now().Add(5 * time.Second)
	for s = h.snapshot(tab); s.Blocker != nil; s = h.snapshot(tab) {
		if time.Now().After(deadline) {
			t.Fatalf("still blocked after closing: %+v", s.Blocker)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// A JavaScript alert is answered for the agent at once, since it only informs, and the click that
// raised it reports what it said: the agent is never left behind a page stopped under it.
func TestAlertIsReportedAndAnswered(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/pop/alert")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Save"), "element": "save"})
	auto, _ := r.Effects["dialogs_auto"].([]any)
	if len(auto) != 1 || !strings.Contains(fmt.Sprint(auto[0]), "message:Saved") || !strings.Contains(fmt.Sprint(auto[0]), "type:alert") {
		t.Fatalf("effects: %+v", r.Effects)
	}
	if s := h.snapshot(tab); s.Title != "after alert" || s.Blocker != nil {
		t.Fatalf("after the alert: %q %+v", s.Title, s.Blocker)
	}
}
