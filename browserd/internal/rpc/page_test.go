//go:build unix

package rpc_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"image/jpeg"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

var update = flag.Bool("update", false, "rewrite the golden snapshots")

func (h *harness) snapshot(tab string) page.Snapshot {
	h.t.Helper()
	var s page.Snapshot
	h.must("page.snapshot", map[string]any{"tab_id": tab}, &s)
	return s
}

// refOf finds the ref of the line with role and name in a snapshot, as an agent reading it would.
func refOf(t *testing.T, s page.Snapshot, role, name string) string {
	t.Helper()
	re := regexp.MustCompile(`(?m)^\s*- ` + regexp.QuoteMeta(role) + ` "` + regexp.QuoteMeta(name) + `" \[ref=([a-z0-9]+)\]`)
	m := re.FindStringSubmatch(s.Text)
	if m == nil {
		t.Fatalf("no %s %q in the snapshot:\n%s", role, name, s.Text)
	}
	return m[1]
}

func (h *harness) act(tab string, params map[string]any) (page.ActResult, error) {
	params["tab_id"] = tab
	var r page.ActResult
	err := h.call("page.act", params, &r)
	return r, err
}

func (h *harness) mustAct(tab string, params map[string]any) page.ActResult {
	h.t.Helper()
	r, err := h.act(tab, params)
	if err != nil {
		h.t.Fatalf("act %v: %v", params, err)
	}
	return r
}

// TestShopToCheckoutByRefs is the scripted agent: search, add to cart, reach the checkout, using
// nothing but the refs of snapshots; the checkout's button is a purchase.
func TestShopToCheckoutByRefs(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/shop/index.html")
	tab := o.Tab.ID
	watcher := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	watcher.waitEvent("hello", 10*time.Second, nil)
	// A live viewer beside the thumbnail, drawing every frame it gets while the agent works.
	live := h.attach("g1", wire.Attach{Tier: "live", MaxW: 1280, MaxH: 800})
	var liveFrames []wire.Frame
	stopLive := make(chan struct{})
	liveDone := make(chan struct{})
	go func() {
		defer close(liveDone)
		for {
			select {
			case f := <-live.frames:
				liveFrames = append(liveFrames, f)
				live.ack(f.FrameNo)
			case <-stopLive:
				return
			}
		}
	}()

	s := h.snapshot(tab)
	if !strings.Contains(s.Text, `- heading "Find something" [level=1]`) || !strings.Contains(s.Text, `- navigation "Categories"`) {
		t.Fatalf("outline:\n%s", s.Text)
	}
	search := refOf(t, s, "searchbox", "Search products")
	r := h.mustAct(tab, map[string]any{"action": "type", "ref": search, "text": "shoes", "submit": true, "element": "the search field"})
	if r.Effects["navigated"] != true || !strings.Contains(fmt.Sprint(r.Effects["url"]), "results.html?q=shoes") {
		t.Fatalf("search: %+v", r)
	}
	if r.Point == nil || r.Box == nil || r.Point.X < r.Box.X || r.Point.X > r.Box.X+r.Box.W {
		t.Fatalf("the point is outside its box: %+v %+v", r.Point, r.Box)
	}
	// The viewer saw the action where it happened.
	a := watcher.waitEvent("action", 5*time.Second, func(e map[string]any) bool { return e["kind"] == "type" })
	if a["text_len"] != float64(5) || a["element"] != "the search field" || a["text"] != nil {
		t.Fatalf("action event: %v", a)
	}
	watcher.waitEvent("action_done", 5*time.Second, nil)

	s = h.snapshot(tab)
	add := refOf(t, s, "button", "Add to cart")
	r = h.mustAct(tab, map[string]any{"action": "click", "ref": add, "element": "Add to cart for the running shoes"})
	if !strings.Contains(r.Diff, "Cart (1)") {
		t.Fatalf("the click's difference: %q", r.Diff)
	}
	if len(r.Sensitive.Kinds) != 0 {
		t.Fatalf("adding to a cart is not a purchase: %v", r.Sensitive.Kinds)
	}
	s = h.snapshot(tab)
	r = h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "link", "Checkout"), "element": "the checkout link"})
	if r.Effects["navigated"] != true {
		t.Fatalf("checkout: %+v", r)
	}
	s = h.snapshot(tab)
	// The card's values are never in what the agent reads.
	if strings.Contains(s.Text, "4111") || strings.Contains(s.Text, "321") {
		t.Fatalf("card values in the snapshot:\n%s", s.Text)
	}
	place := refOf(t, s, "button", "Place order")
	dry := h.mustAct(tab, map[string]any{"action": "click", "ref": place, "element": "Place order", "dry_run": true})
	if !slices.Contains(dry.Sensitive.Kinds, "purchase") {
		t.Fatalf("Place order: %+v", dry.Sensitive)
	}
	close(stopLive)
	<-liveDone
	if len(liveFrames) < 3 {
		t.Fatalf("the live viewer got %d frames through three pages", len(liveFrames))
	}
	for i, f := range liveFrames {
		if f.FrameNo != uint32(i+1) || f.Meta.Tier != "live" || f.Meta.W != 1280 || (i > 0 && f.Meta.TS < liveFrames[i-1].Meta.TS) {
			t.Fatalf("live frame %d: %+v", i, f.Meta)
		}
	}
	if dry.Effects["navigated"] != nil || dry.Point == nil {
		t.Fatalf("a dry run acted or did not aim: %+v", dry)
	}
	// And typing into the card field is the operator's.
	_, err := h.act(tab, map[string]any{"action": "type", "ref": refOf(t, s, "textbox", "Card number"), "text": "1", "element": "card"})
	if code(err) != 1105 {
		t.Fatalf("typing into a card field: %v", err)
	}
	watcher.waitEvent("needs_you", 5*time.Second, func(e map[string]any) bool { return e["reason"] == "field_forbidden" })
}

var secretValues = []string{"SECRET-password-1", "SECRET-otp-2", "SECRET-new-3", "SECRET-otp-4"}

func TestSecretsNeverLeaveThePage(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/login.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	var text page.Text
	h.must("page.text", map[string]any{"tab_id": tab}, &text)
	for _, v := range secretValues {
		if strings.Contains(s.Text, v) || strings.Contains(text.Text, v) {
			t.Fatalf("%s leaked:\n%s\n%s", v, s.Text, text.Text)
		}
	}
	for _, name := range []string{"Password", "One-time code", "New password"} {
		if !regexp.MustCompile(`- textbox "` + name + `" \[ref=\w+\].*\[secret\]`).MatchString(s.Text) {
			t.Fatalf("%s not marked secret:\n%s", name, s.Text)
		}
	}
	if !strings.Contains(s.Text, `value="someone@example.com"`) {
		t.Fatalf("an ordinary field's value is missing:\n%s", s.Text)
	}
	pw := refOf(t, s, "textbox", "Password")
	for _, a := range []map[string]any{
		{"action": "type", "ref": pw, "text": "guess", "element": "password"},
		{"action": "press", "ref": pw, "keys": "a", "element": "password"},
	} {
		if _, err := h.act(tab, a); code(err) != 1105 {
			t.Fatalf("%v: %v", a, err)
		}
	}
	// Enter in a password field is allowed; it is the submit, and a credentials ask for the host.
	dry := h.mustAct(tab, map[string]any{"action": "press", "ref": pw, "keys": "Enter", "element": "submit", "dry_run": true})
	if !slices.Contains(dry.Sensitive.Kinds, "credentials") {
		t.Fatalf("Enter in a login form: %+v", dry.Sensitive)
	}
	dry = h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Sign in"), "element": "Sign in", "dry_run": true})
	if !slices.Contains(dry.Sensitive.Kinds, "credentials") {
		t.Fatalf("Sign in: %+v", dry.Sensitive)
	}
	dry = h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Send it over"), "element": "send", "dry_run": true})
	if !slices.Contains(dry.Sensitive.Kinds, "cross_origin_post") || !slices.Contains(dry.Sensitive.Kinds, "send") {
		t.Fatalf("a form to another site: %+v", dry.Sensitive)
	}
	// A screenshot hides every secret field: the big one-time-code field is one grey block.
	var shot page.Screenshot
	h.must("page.screenshot", map[string]any{"tab_id": tab, "format": "jpeg", "quality": 90}, &shot)
	if len(shot.Masked) < 4 {
		t.Fatalf("masked: %v", shot.Masked)
	}
	raw, _ := base64.StdEncoding.DecodeString(shot.Data)
	img, err := jpeg.Decode(bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	for _, pt := range [][2]int{{60, 470}, {220, 480}, {380, 520}} {
		r, g, b, _ := img.At(pt[0], pt[1]).RGBA()
		for _, c := range []uint32{r >> 8, g >> 8, b >> 8} {
			if c < 0x9e-12 || c > 0x9e+12 {
				t.Fatalf("pixel %v is %d,%d,%d, not the mask's grey", pt, r>>8, g>>8, b>>8)
			}
		}
	}
	// The masks are gone after the capture.
	var after page.Screenshot
	h.must("page.screenshot", map[string]any{"tab_id": tab, "ref": refOf(t, s, "textbox", "Email"), "format": "png"}, &after)
	s2 := h.snapshot(tab)
	if strings.Contains(s2.Text, "SECRET") || !strings.Contains(s2.Text, `value="someone@example.com"`) {
		t.Fatalf("after the screenshot:\n%s", s2.Text)
	}
}

func TestWidgetsFramesShadowAndRefs(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/widgets.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	for _, want := range []string{`- combobox "Size" [ref=`, `value="Small"`, `- checkbox "Keep me posted" [ref=`,
		`- button "Inside the shadow" [ref=`, `- iframe "Inner frame" [ref=f`, `- button "In the frame" [ref=f`} {
		if !strings.Contains(s.Text, want) {
			t.Fatalf("missing %s in:\n%s", want, s.Text)
		}
	}
	for _, hidden := range []string{"keyproxy", "Hidden from the outline"} {
		if strings.Contains(s.Text, hidden) {
			t.Fatalf("hidden text %q in the outline:\n%s", hidden, s.Text)
		}
	}
	if len(s.Frames) != 1 || s.Frames[0].CrossOrigin {
		t.Fatalf("frames: %+v", s.Frames)
	}
	// Refs hold across snapshots and across a re-render that keeps the element.
	bump := refOf(t, s, "button", "Bump")
	h.mustAct(tab, map[string]any{"action": "click", "ref": bump, "element": "Bump"})
	s2 := h.snapshot(tab)
	if refOf(t, s2, "button", "Bump") != bump || !strings.Contains(s2.Text, "Count 1") {
		t.Fatalf("after a re-render:\n%s", s2.Text)
	}
	// select, check, a click in a shadow root and one in a frame.
	r := h.mustAct(tab, map[string]any{"action": "select", "ref": refOf(t, s, "combobox", "Size"), "option": "Large", "element": "size"})
	if !strings.Contains(r.Diff, `value="Large"`) {
		t.Fatalf("select diff: %q", r.Diff)
	}
	if _, err := h.act(tab, map[string]any{"action": "select", "ref": refOf(t, s, "combobox", "Size"), "option": "Huge", "element": "size"}); code(err) != -32602 {
		t.Fatalf("a missing option: %v", err)
	}
	h.mustAct(tab, map[string]any{"action": "check", "ref": refOf(t, s, "checkbox", "Keep me posted"), "element": "posted"})
	again := h.mustAct(tab, map[string]any{"action": "check", "ref": refOf(t, s, "checkbox", "Keep me posted"), "element": "posted"})
	if again.Effects["unchanged"] != true {
		t.Fatalf("checking a checked box: %+v", again)
	}
	title := func() string {
		var list struct {
			Tabs []map[string]any `json:"tabs"`
		}
		h.must("tab.list", map[string]any{"group_id": "g1"}, &list)
		return fmt.Sprint(list.Tabs[0]["title"])
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Inside the shadow"), "element": "shadow"})
	if title() != "shadow clicked" {
		t.Fatalf("shadow click: %s", title())
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "In the frame"), "element": "frame"})
	if title() != "frame clicked" {
		t.Fatalf("frame click: %s", title())
	}
	// A covered element is not clicked: whatever covers it would be.
	if _, err := h.act(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Covered button"), "element": "covered"}); code(err) != 1004 {
		t.Fatalf("a covered click: %v", err)
	}
	// A stale ref.
	h.must("page.reload", map[string]any{"tab_id": tab}, nil)
	if _, err := h.act(tab, map[string]any{"action": "click", "ref": bump, "element": "Bump"}); code(err) != 1103 {
		t.Fatalf("a ref from the previous document: %v", err)
	}
	// Waiting for text that arrives later.
	s = h.snapshot(tab)
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Later"), "element": "later"})
	var w map[string]any
	h.must("page.wait", map[string]any{"tab_id": tab, "for": "text", "value": "Arrived late", "timeout_ms": 5000}, &w)
	if w["matched"] != "text" {
		t.Fatalf("wait: %v", w)
	}
	h.must("page.wait", map[string]any{"tab_id": tab, "for": "text", "value": "Never", "timeout_ms": 300}, &w)
	if w["matched"] != "timeout" {
		t.Fatalf("wait for nothing: %v", w)
	}
	// Keys and scrolling.
	h.mustAct(tab, map[string]any{"action": "scroll", "direction": "down", "element": "the page"})
	h.mustAct(tab, map[string]any{"action": "press", "keys": "Ctrl+A", "element": "select all"})
	if _, err := h.act(tab, map[string]any{"action": "press", "keys": "Hyper+A", "element": "x"}); code(err) != -32602 {
		t.Fatalf("an unknown modifier: %v", err)
	}
	if _, err := h.act(tab, map[string]any{"action": "click", "ref": "e1"}); code(err) != -32602 {
		t.Fatalf("an action without its element description: %v", err)
	}
}

func TestDialogBlocksThePage(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/widgets.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Ask"), "element": "ask"})
	d, _ := r.Effects["dialog"].(map[string]any)
	if d == nil || d["type"] != "confirm" || d["message"] != "Leave?" {
		t.Fatalf("effects: %+v", r.Effects)
	}
	if err := h.call("page.snapshot", map[string]any{"tab_id": tab}, nil); code(err) != 1107 {
		t.Fatalf("a snapshot under a dialog: %v", err)
	}
	if err := h.call("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + "/still"}, nil); code(err) != 1107 {
		t.Fatalf("a navigation under a dialog: %v", err)
	}
	h.must("dialog.answer", map[string]any{"tab_id": tab, "accept": true}, nil)
	if err := h.call("dialog.answer", map[string]any{"tab_id": tab, "accept": true}, nil); code(err) != 1001 {
		t.Fatalf("no dialog: %v", err)
	}
	s = h.snapshot(tab)
	if s.Title != "yes" {
		t.Fatalf("title after accepting: %q", s.Title)
	}
}

func TestDownloadAndUpload(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/widgets.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "link", "Get the report"), "element": "report"})
	var dl struct {
		Downloads []page.Download `json:"downloads"`
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		h.must("download.list", map[string]any{"group_id": "g1"}, &dl)
		if len(dl.Downloads) == 1 && dl.Downloads[0].State == "completed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("downloads: %+v (action %+v)", dl, r.Effects)
		}
		time.Sleep(100 * time.Millisecond)
	}
	d := dl.Downloads[0]
	content := "the quarterly report\n"
	sum := sha256.Sum256([]byte(content))
	if d.Name != "report.txt" || d.Size != int64(len(content)) || d.SHA256 != hex.EncodeToString(sum[:]) || d.TabID != tab {
		t.Fatalf("download: %+v", d)
	}
	var chunk map[string]any
	h.must("download.read", map[string]any{"id": d.ID, "offset": 0}, &chunk)
	data, _ := base64.StdEncoding.DecodeString(fmt.Sprint(chunk["data_b64"]))
	if string(data) != content || chunk["eof"] != true {
		t.Fatalf("read: %v", chunk)
	}

	// An upload in two chunks, then set on the file input.
	part1 := base64.StdEncoding.EncodeToString([]byte("hello "))
	part2 := base64.StdEncoding.EncodeToString([]byte("world"))
	var up struct {
		UploadID string `json:"upload_id"`
		Size     int64  `json:"size"`
	}
	h.must("upload.put", map[string]any{"group_id": "g1", "name": "note.txt", "offset": 0, "data_b64": part1}, &up)
	if err := h.call("upload.put", map[string]any{"upload_id": up.UploadID, "group_id": "g1", "name": "note.txt", "offset": 3, "data_b64": part2}, nil); code(err) != -32602 {
		t.Fatalf("a chunk at the wrong offset: %v", err)
	}
	h.must("upload.put", map[string]any{"upload_id": up.UploadID, "group_id": "g1", "name": "note.txt", "offset": 6, "data_b64": part2}, &up)
	if up.Size != 11 {
		t.Fatalf("upload: %+v", up)
	}
	if err := h.call("upload.put", map[string]any{"group_id": "g1", "name": "../etc/passwd", "offset": 0, "data_b64": part1}, nil); code(err) != -32602 {
		t.Fatalf("a name with a path: %v", err)
	}
	upRef := refOf(t, s, "button", "Choose File")
	if dry := h.mustAct(tab, map[string]any{"action": "click", "ref": upRef, "element": "?", "dry_run": true}); dry.Element == nil || !dry.Element.File {
		t.Fatalf("not a file input: %+v", dry.Element)
	}
	r = h.mustAct(tab, map[string]any{"action": "upload", "ref": upRef, "upload_ids": []string{up.UploadID}, "element": "the file input"})
	if !slices.Contains(r.Sensitive.Kinds, "upload") {
		t.Fatalf("an upload is sensitive: %+v", r.Sensitive)
	}
	if st := h.snapshot(tab); st.Title != "got note.txt 11" {
		t.Fatalf("the page's view of the upload: %q", st.Title)
	}
}

func TestOperatorIsCalledForAuthAndCaptcha(t *testing.T) {
	h := startWith(t, nil, []string{"--host-resolver-rules=MAP hcaptcha.com 127.0.0.1"})
	if err := h.client.Call(t.Context(), "events.subscribe", map[string]any{"after_seq": 0}, nil); err != nil {
		t.Fatal(err)
	}
	o := h.open("g1", "project-a", "/still")
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/private"}, nil)
	e, err := h.client.WaitEvent("needs_you", "", 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var data map[string]any
	_ = json.Unmarshal(e.Data, &data)
	if data["reason"] != "basic_auth" || data["by"] != "daemon" {
		t.Fatalf("needs_you: %s", e.Data)
	}
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/site/captcha.html"}, nil)
	e, err = h.client.WaitEvent("needs_you", "", 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(e.Data, &data)
	if data["reason"] != "captcha" {
		t.Fatalf("needs_you: %s", e.Data)
	}
}

func TestHumanTypedFieldsBecomeSecret(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/shop/checkout.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	name := refOf(t, s, "textbox", "Full name")
	// Focus the field, then a person types into it.
	h.mustAct(tab, map[string]any{"action": "click", "ref": name, "element": "name"})
	v := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	v.waitEvent("hello", 10*time.Second, nil)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, nil)
	v.input(map[string]any{"t": "text", "text": "Private Person"})
	time.Sleep(500 * time.Millisecond)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "agent"}, nil)
	s = h.snapshot(tab)
	if strings.Contains(s.Text, "Private Person") || !regexp.MustCompile(`- textbox "Full name" \[ref=\w+\].*\[secret\]`).MatchString(s.Text) {
		t.Fatalf("what a person typed is readable:\n%s", s.Text)
	}
	if _, err := h.act(tab, map[string]any{"action": "type", "ref": name, "text": "x", "element": "name"}); code(err) != 1105 {
		t.Fatalf("typing over what a person typed: %v", err)
	}
}

func TestNoSecretInAnyOutput(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/secrets")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	var text page.Text
	h.must("page.text", map[string]any{"tab_id": tab}, &text)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Change"), "element": "change"})
	var scoped page.Snapshot
	h.must("page.snapshot", map[string]any{"tab_id": tab, "scope_ref": s.Frames[0].Ref}, &scoped)
	outputs := map[string]string{"snapshot": s.Text, "text": text.Text, "diff": r.Diff, "frame snapshot": scoped.Text}
	for name, out := range outputs {
		if strings.Contains(out, "SECRET") {
			t.Errorf("a secret value in the %s:\n%s", name, out)
		}
	}
	if n := strings.Count(s.Text, "[secret]"); n != len(secretFields)+3 {
		t.Errorf("%d fields marked secret, want %d:\n%s", n, len(secretFields)+3, s.Text)
	}
	if !strings.Contains(r.Diff, "changed") {
		t.Errorf("the difference: %q", r.Diff)
	}
}

// The outline of a known page, held as a golden file: a change to how pages are read shows as a
// change to this file, reviewed like code. -update rewrites it. The long page's geometry is fixed in
// pixels, so its scroll figures are the same in every Chromium. Each page is its browser's first:
// the header says the viewport's size, and Debian's Chromium gives a browser's later windows a page
// taller than the group's viewport (the pinned build does not), which is the window sizing's matter,
// not the outline's.
func TestSnapshotGolden(t *testing.T) {
	h := start(t, nil)
	for _, c := range []struct {
		path, golden string
		params       map[string]any
		before       map[string]any // an action taken first
	}{
		{path: "/site/shop/index.html", golden: "testdata/golden/shop-index.txt"},
		{path: "/site/shop/checkout.html", golden: "testdata/golden/shop-checkout.txt"},
		{path: "/site/widgets.html", golden: "testdata/golden/widgets.txt"},
		{path: "/site/clickables.html", golden: "testdata/golden/clickables.txt"},
		{path: "/site/long.html", golden: "testdata/golden/long-cut.txt", params: map[string]any{"max_chars": 1500}},
		{path: "/site/long.html", golden: "testdata/golden/long-viewport.txt", params: map[string]any{"view": "viewport"},
			before: map[string]any{"action": "scroll", "text": "Section 10", "element": "section 10"}},
	} {
		o := h.open("golden", "golden", c.path)
		if c.before != nil {
			h.mustAct(o.Tab.ID, c.before)
		}
		params := map[string]any{"tab_id": o.Tab.ID}
		for k, v := range c.params {
			params[k] = v
		}
		var s page.Snapshot
		h.must("page.snapshot", params, &s)
		got := strings.ReplaceAll(s.Text, h.site.URL, "http://SITE")
		var list struct {
			Browsers []struct {
				ID      string `json:"id"`
				Profile string `json:"profile"`
			} `json:"browsers"`
		}
		h.must("browser.list", map[string]any{}, &list)
		for _, b := range list.Browsers {
			if b.Profile == "golden" {
				h.must("browser.close", map[string]any{"browser_id": b.ID}, nil)
			}
		}
		if *update {
			if err := os.MkdirAll(filepath.Dir(c.golden), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(c.golden, []byte(got+"\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(c.golden)
		if err != nil {
			t.Fatal(err)
		}
		if got+"\n" != string(want) {
			t.Errorf("%s:\n got:\n%s\nwant:\n%s", c.golden, got, want)
		}
	}
}
