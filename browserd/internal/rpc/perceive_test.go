//go:build unix

package rpc_test

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image/jpeg"
	"net/http/httptest"
	"net/netip"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/browserd/internal/page"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

func (h *harness) title(group string) string {
	h.t.Helper()
	var list struct {
		Tabs []map[string]any `json:"tabs"`
	}
	h.must("tab.list", map[string]any{"group_id": group}, &list)
	return fmt.Sprint(list.Tabs[0]["title"])
}

// waitTitle waits for a page's title, which its handlers set a moment after the input.
func (h *harness) waitTitle(group, want string) {
	h.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for h.title(group) != want {
		if time.Now().After(deadline) {
			h.t.Fatalf("title %q, want %q", h.title(group), want)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

var paneRef = regexp.MustCompile(`generic \[ref=(e\d+)\] \[scrollable\]`)

// What a script made clickable is read as clickable and acts as such: a card with a listener, a
// span with a pointer, a checkbox and a file input drawn by their labels, a select's options, a pane
// that scrolls on its own.
func TestClickablesLabelsAndPanes(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/clickables.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "generic", "Open the plain card"), "element": "the plain card"})
	h.waitTitle("g1", "plain clicked")
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "generic", "Save"), "element": "the heart"})
	h.waitTitle("g1", "saved")

	dark := refOf(t, s, "checkbox", "Dark mode")
	h.mustAct(tab, map[string]any{"action": "check", "ref": dark, "element": "dark mode"})
	h.waitTitle("g1", "dark true")
	if again := h.mustAct(tab, map[string]any{"action": "check", "ref": dark, "element": "dark mode"}); again.Effects["unchanged"] != true {
		t.Fatalf("checking a checked box through its label: %+v", again)
	}

	var up struct {
		UploadID string `json:"upload_id"`
	}
	h.must("upload.put", map[string]any{"group_id": "g1", "name": "note.txt", "offset": 0, "data_b64": base64.StdEncoding.EncodeToString([]byte("hi"))}, &up)
	h.mustAct(tab, map[string]any{"action": "upload", "ref": refOf(t, s, "button", "Upload a picture"), "upload_ids": []string{up.UploadID}, "element": "the picture"})
	h.waitTitle("g1", "photo note.txt")

	// An option past the ones the line shows is chosen by its label all the same.
	r := h.mustAct(tab, map[string]any{"action": "select", "ref": refOf(t, s, "combobox", "Country"), "option": "Japan", "element": "country"})
	if !strings.Contains(r.Diff, `value="Japan"`) {
		t.Fatalf("select: %q", r.Diff)
	}

	// A menu that opens on hover has a ref to hover, and its items show once it is open.
	menu := refOf(t, s, "generic", "Account")
	if strings.Contains(s.Text, "Profile") || !strings.Contains(s.Text, `generic "Account" [ref=`+menu+`] [collapsed]`) {
		t.Fatalf("the closed menu:\n%s", s.Text)
	}
	h.mustAct(tab, map[string]any{"action": "hover", "ref": menu, "element": "the account menu"})
	open := h.snapshot(tab)
	r = h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, open, "link", "Profile"), "element": "profile"})
	if r.Effects["navigated"] != true {
		t.Fatalf("the menu's item: %+v", r.Effects)
	}
	h.must("page.back", map[string]any{"tab_id": tab}, nil)
	s = h.snapshot(tab)

	m := paneRef.FindStringSubmatch(s.Text)
	if m == nil {
		t.Fatalf("no scrollable pane in:\n%s", s.Text)
	}
	for i := 0; ; i++ {
		r = h.mustAct(tab, map[string]any{"action": "scroll", "ref": m[1], "direction": "down", "element": "the pane"})
		sc, _ := r.Effects["scrolled"].(map[string]any)
		if sc == nil || (i == 0 && sc["by"].(float64) <= 0) || sc["pane"] != m[1] {
			t.Fatalf("scrolling the pane: %+v", r.Effects)
		}
		if sc["at_end"] == true {
			break
		}
		if i > 5 {
			t.Fatalf("the pane never reached its end: %+v", r.Effects)
		}
	}
	if _, ok := r.Effects["scroll"].(map[string]any); !ok {
		t.Fatalf("no page scroll state after a scroll: %+v", r.Effects)
	}
}

// The header says where the page is and how much is left; scrolling to a text brings it into view;
// the viewport view reads what is on screen; a search finds text with the controls beside it; and
// a cut outline stays inside its budget and says where the page goes on.
func TestHeaderScrollFindAndCut(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/long.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	if s.Scroll.Above != 0 || s.Scroll.Below < 9 || s.Viewport.W != 1280 || !strings.HasPrefix(s.Text, "- [viewport 1280x") {
		t.Fatalf("header: %+v\n%s", s.Scroll, s.Text)
	}
	r := h.mustAct(tab, map[string]any{"action": "scroll", "text": "section 15", "element": "section 15"})
	if r.Effects["found"] == nil || r.Box == nil || r.Box.Y < 0 || r.Box.Y > 800 {
		t.Fatalf("scroll to a text: %+v %+v", r.Effects, r.Box)
	}
	if _, err := h.act(tab, map[string]any{"action": "scroll", "text": "no such words anywhere", "element": "x"}); code(err) != 1001 {
		t.Fatalf("scrolling to a missing text: %v", err)
	}
	var v page.Snapshot
	h.must("page.snapshot", map[string]any{"tab_id": tab, "view": "viewport"}, &v)
	if !strings.Contains(v.Text, `heading "Section 15"`) || strings.Contains(v.Text, `heading "Section 2"`) || !strings.Contains(v.Text, "screens above") {
		t.Fatalf("viewport view:\n%s", v.Text)
	}
	if err := h.call("page.snapshot", map[string]any{"tab_id": tab, "view": "everything"}, nil); code(err) != -32602 {
		t.Fatalf("an unknown view: %v", err)
	}

	var f page.Found
	h.must("page.find", map[string]any{"tab_id": tab, "query": `^Section 1\d$`, "regex": true}, &f)
	if f.Total != 10 || len(f.Matches) != 10 || len(f.Matches[0].Near) == 0 || f.Matches[0].Near[0].Name != "More on 10" {
		t.Fatalf("find: %+v", f)
	}
	h.must("page.find", map[string]any{"tab_id": tab, "query": "more on 7", "max": 3}, &f)
	if f.Total != 1 || f.Matches[0].In == nil || f.Matches[0].In.Role != "link" || !strings.Contains(f.Text, "[ref=") {
		t.Fatalf("find in a link: %+v", f)
	}
	if err := h.call("page.find", map[string]any{"tab_id": tab, "query": "(", "regex": true}, nil); code(err) != -32602 {
		t.Fatalf("a bad pattern: %v", err)
	}
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + "/backtrack"}, nil)
	began := time.Now()
	if err := h.call("page.find", map[string]any{"tab_id": tab, "query": "(a+)+$", "regex": true}, nil); code(err) != -32602 {
		t.Fatalf("a pattern that never ends: %v", err)
	}
	if time.Since(began) > 10*time.Second {
		t.Fatalf("the search ran %s", time.Since(began))
	}
	// The page lives on after its search was stopped.
	h.snapshot(tab)

	h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + "/site/long.html"}, nil)
	for _, n := range []int{1000, 1500, 2500} {
		var c page.Snapshot
		h.must("page.snapshot", map[string]any{"tab_id": tab, "max_chars": n}, &c)
		if !c.Truncated || len(c.Text) > n || !strings.Contains(c.Text, "not shown:") {
			t.Fatalf("cut at %d: %d characters, truncated %v:\n%s", n, len(c.Text), c.Truncated, c.Text)
		}
	}
	// A heading's scope is its section.
	var c page.Snapshot
	h.must("page.snapshot", map[string]any{"tab_id": tab, "max_chars": 1000}, &c)
	m := regexp.MustCompile(`section "Section (\d+)" \[ref=(e\d+)\]`).FindStringSubmatch(c.Text)
	if m == nil {
		t.Fatalf("no section to scope to:\n%s", c.Text)
	}
	h.must("page.snapshot", map[string]any{"tab_id": tab, "scope_ref": m[2]}, &c)
	if !strings.Contains(c.Text, `heading "Section `+m[1]+`"`) || !strings.Contains(c.Text, "More on "+m[1]) || strings.Contains(c.Text, "viewport") {
		t.Fatalf("the scoped section:\n%s", c.Text)
	}
}

// Acting at a point is off unless the host turns it on for the session; on, the element found at the
// point is classified and hit-tested as a ref's is.
func TestPointClicks(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/site/shop/checkout.html")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	place := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Place order"), "element": "place", "dry_run": true})
	at := map[string]any{"action": "click", "x": place.Point.X, "y": place.Point.Y, "element": "the button at the point", "dry_run": true}
	if _, err := h.act(tab, at); code(err) != 1004 {
		t.Fatalf("a point without the session's switch: %v", err)
	}
	at["allow_point"] = true
	dry := h.mustAct(tab, at)
	if dry.Element == nil || dry.Element.Name != "Place order" || !slices.Contains(dry.Sensitive.Kinds, "purchase") || dry.Ref == "" {
		t.Fatalf("the element at the point: %+v %+v", dry.Element, dry.Sensitive)
	}
	for _, bad := range []map[string]any{
		{"action": "type", "x": 10, "y": 10, "text": "x", "allow_point": true, "element": "x"},
		{"action": "click", "x": 10, "allow_point": true, "element": "x"},
		{"action": "click", "x": 5000, "y": 10, "allow_point": true, "element": "x"},
		{"action": "click", "x": 10, "y": 10, "ref": "e1", "allow_point": true, "element": "x"},
	} {
		if _, err := h.act(tab, bad); code(err) != -32602 {
			t.Fatalf("%v: %v", bad, err)
		}
	}
	// A real click at the point of the name field focuses it, as a click on its ref would.
	name := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "textbox", "Full name"), "element": "name", "dry_run": true})
	h.mustAct(tab, map[string]any{"action": "click", "x": name.Point.X, "y": name.Point.Y, "allow_point": true, "element": "the name field"})
	if s := h.snapshot(tab); !regexp.MustCompile(`textbox "Full name" \[ref=\w+\] \[focused\]`).MatchString(s.Text) {
		t.Fatalf("the point click did not reach the field:\n%s", s.Text)
	}
}

// crossSites starts the daemon with a wall that sends four names to the fixture: the page's site,
// another site for a payment frame and a frame inside it, and a subdomain of the page's site (another
// origin, the same site, so Chromium keeps it in the page's process).
func crossSites(t *testing.T) *harness {
	h := startWall(t, nil, nil, func(site *httptest.Server, publish func(netwall.Egress)) *netwall.Browsers {
		w := netwall.New(netwall.Options{Events: publish, Resolver: publicNames{"shop.example": "93.184.216.34",
			"pay.example": "93.184.216.35", "deep.example": "93.184.216.36", "cdn.shop.example": "93.184.216.37"}})
		fixtureAddr := netip.MustParseAddrPort(site.Listener.Addr().String())
		w.SetTestRedirect(func(netip.AddrPort) netip.AddrPort { return fixtureAddr })
		return netwall.NewBrowsers(w)
	})
	h.must("net.configure", map[string]any{"services_ports": [][2]int{}, "ask_loopback": false, "lan_allow": []string{}}, nil)
	return h
}

// A frame of another site is read in a world of its own and spliced into the outline, its refs
// prefixed with the frame's; actions reach it with its offset, its secrets stay the operator's and
// are masked in screenshots, and a person's typing in it becomes secret.
func TestFramesOfOtherSites(t *testing.T) {
	h := crossSites(t)
	o := h.open("g1", "project-a", "")
	tab := o.Tab.ID
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": "http://shop.example/xsite/outer"}, nil)
	var s page.Snapshot
	deadline := time.Now().Add(10 * time.Second)
	for {
		s = h.snapshot(tab)
		if strings.Contains(s.Text, "Deep button") && strings.Contains(s.Text, "Widget button") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the frames were not read:\n%s", s.Text)
		}
		time.Sleep(200 * time.Millisecond)
	}
	if strings.Contains(s.Text, "SECRET") {
		t.Fatalf("a frame's secret in the outline:\n%s", s.Text)
	}
	for _, want := range []*regexp.Regexp{
		regexp.MustCompile(`(?m)^  - textbox "Card" \[ref=f\d+e\d+\] \[secret\]$`),
		regexp.MustCompile(`(?m)^  - button "Pay now" \[ref=f\d+e\d+\]$`),
		regexp.MustCompile(`(?m)^    - button "Deep button" \[ref=f\d+f\d+e\d+\]$`),
		regexp.MustCompile(`(?m)^  - button "Widget button" \[ref=f\d+e\d+\]$`),
	} {
		if !want.MatchString(s.Text) {
			t.Fatalf("%s not in:\n%s", want, s.Text)
		}
	}
	read := 0
	for _, f := range s.Frames {
		if f.CrossOrigin && f.Read {
			read++
		}
	}
	if read != 3 {
		t.Fatalf("frames: %+v", s.Frames)
	}
	if ft, err := h.manager.Tab(tab); err != nil || ft.FramesApart() < 2 {
		t.Fatalf("the frames of other sites are not in processes of their own: %v", err)
	}

	pay := refOf(t, s, "button", "Pay now")
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": pay, "element": "pay"})
	if !strings.Contains(r.Diff, `text "paid"`) {
		t.Fatalf("the click in the frame: %q", r.Diff)
	}
	h.mustAct(tab, map[string]any{"action": "type", "ref": refOf(t, s, "textbox", "Note"), "text": "leave at the door", "element": "note"})
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Deep button"), "element": "deep"})
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Widget button"), "element": "widget"})
	s2 := h.snapshot(tab)
	for _, want := range []string{`value="leave at the door"`, "deep clicked", "widget clicked"} {
		if !strings.Contains(s2.Text, want) {
			t.Fatalf("%s not in:\n%s", want, s2.Text)
		}
	}
	card := refOf(t, s, "textbox", "Card")
	if _, err := h.act(tab, map[string]any{"action": "type", "ref": card, "text": "4111", "element": "card"}); code(err) != 1105 {
		t.Fatalf("typing into a frame's card field: %v", err)
	}

	// Scoped to the frame, and searched.
	frameRef := regexp.MustCompile(`iframe "Payment" \[ref=(f\d+)\]`).FindStringSubmatch(s.Text)[1]
	var scoped page.Snapshot
	h.must("page.snapshot", map[string]any{"tab_id": tab, "scope_ref": frameRef}, &scoped)
	if !strings.Contains(scoped.Text, "Pay now") || strings.Contains(scoped.Text, "Checkout") || strings.Contains(scoped.Text, "SECRET") {
		t.Fatalf("the frame scoped:\n%s", scoped.Text)
	}
	var f page.Found
	h.must("page.find", map[string]any{"tab_id": tab, "query": "pay now"}, &f)
	if f.Total != 1 || f.Matches[0].In == nil || f.Matches[0].In.Ref != pay {
		t.Fatalf("find in a frame: %+v", f)
	}
	h.must("page.find", map[string]any{"tab_id": tab, "query": "SECRET"}, &f)
	if f.Total != 0 {
		t.Fatalf("a search found a secret value: %+v", f)
	}

	// A point inside the frame finds the frame's element.
	dry := h.mustAct(tab, map[string]any{"action": "click", "ref": pay, "element": "pay", "dry_run": true})
	at := h.mustAct(tab, map[string]any{"action": "click", "x": dry.Point.X, "y": dry.Point.Y, "allow_point": true, "element": "pay", "dry_run": true})
	if at.Ref != pay {
		t.Fatalf("the point found %q, not %q", at.Ref, pay)
	}

	// The screenshot masks the frame's secret fields.
	cardBox := h.mustAct(tab, map[string]any{"action": "click", "ref": card, "element": "card", "dry_run": true}).Box
	var shot page.Screenshot
	h.must("page.screenshot", map[string]any{"tab_id": tab, "format": "jpeg", "quality": 90}, &shot)
	if len(shot.Masked) < 2 {
		t.Fatalf("masked: %v", shot.Masked)
	}
	raw, _ := base64.StdEncoding.DecodeString(shot.Data)
	img, err := jpeg.Decode(bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	px, py := int(cardBox.X+cardBox.W/2), int(cardBox.Y+cardBox.H/2)
	cr, cg, cb, _ := img.At(px, py).RGBA()
	for _, c := range []uint32{cr >> 8, cg >> 8, cb >> 8} {
		if c < 0x9e-12 || c > 0x9e+12 {
			t.Fatalf("the frame's card field at %d,%d is %d,%d,%d, not the mask's grey", px, py, cr>>8, cg>>8, cb>>8)
		}
	}

	// A person types into the frame's note field: from then on it is theirs.
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "textbox", "Note"), "element": "note"})
	v := h.attach("g1", wire.Attach{Tier: "thumb", MaxW: 320, MaxH: 200})
	v.waitEvent("hello", 10*time.Second, nil)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "human", "client_id": v.id}, nil)
	v.input(map[string]any{"t": "text", "text": " private"})
	time.Sleep(500 * time.Millisecond)
	h.must("control.set", map[string]any{"group_id": "g1", "owner": "agent"}, nil)
	s3 := h.snapshot(tab)
	if strings.Contains(s3.Text, "private") || !regexp.MustCompile(`textbox "Note" \[ref=\w+\].*\[secret\]`).MatchString(s3.Text) {
		t.Fatalf("what a person typed in a frame is readable:\n%s", s3.Text)
	}
}

// A small control in a frame that sits centred and bordered on the page is clicked where it is.
func TestSmallControlsInACentredFrame(t *testing.T) {
	h := crossSites(t)
	o := h.open("g1", "project-a", "")
	tab := o.Tab.ID
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": "http://shop.example/xsite/centred"}, nil)
	var s page.Snapshot
	deadline := time.Now().Add(10 * time.Second)
	for s = h.snapshot(tab); !strings.Contains(s.Text, "Pay yearly"); s = h.snapshot(tab) {
		if time.Now().After(deadline) {
			t.Fatalf("the frame was not read:\n%s", s.Text)
		}
		time.Sleep(200 * time.Millisecond)
	}
	// As the host does it before a question: a dry run, a picture of the element, then the click. A
	// click right after a screenshot is where Chromium used to send the input to the page around the
	// frame, every time.
	for i, name := range []string{"Pay yearly", "Pay monthly", "Pay yearly"} {
		ref := refOf(t, s, "radio", name)
		h.mustAct(tab, map[string]any{"action": "click", "ref": ref, "element": name, "dry_run": true})
		var shot page.Screenshot
		h.must("page.screenshot", map[string]any{"tab_id": tab, "ref": ref, "max_width": 320, "format": "jpeg", "quality": 60}, &shot)
		r := h.mustAct(tab, map[string]any{"action": "click", "ref": ref, "element": name})
		if !strings.Contains(r.Diff, `radio "`+name+`" [ref=`+ref+`] [checked]`) {
			t.Fatalf("click %d: the radio in the frame did not change: %q (point %+v, box %+v)\n%s", i, r.Diff, r.Point, r.Box, h.snapshot(tab).Text)
		}
	}
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Save"), "element": "save"})
	if s := h.snapshot(tab); !strings.Contains(s.Text, "saved yearly") {
		t.Fatalf("after saving:\n%s", s.Text)
	}
}

// axRoles are the roles of the accessibility tree whose nodes the outline must show with the same
// role and name.
var axRoles = map[string]bool{"button": true, "link": true, "textbox": true, "searchbox": true, "checkbox": true,
	"radio": true, "combobox": true, "switch": true, "slider": true, "spinbutton": true, "tab": true, "menuitem": true}

// The outline's roles and names are the accessibility tree's: every control Chromium's own tree
// names is on a line of the outline with that role and name.
func TestOutlineAgreesWithTheAccessibilityTree(t *testing.T) {
	h := start(t, nil)
	for i, path := range []string{"/site/shop/index.html", "/site/shop/checkout.html", "/site/login.html", "/site/widgets.html", "/site/clickables.html"} {
		o := h.open(fmt.Sprintf("ax%d", i), "project-a", path)
		s := h.snapshot(o.Tab.ID)
		tab, err := h.manager.Tab(o.Tab.ID)
		if err != nil {
			t.Fatal(err)
		}
		var tree struct {
			Nodes []struct {
				Ignored bool `json:"ignored"`
				Role    struct {
					Value string `json:"value"`
				} `json:"role"`
				Name struct {
					Value string `json:"value"`
				} `json:"name"`
			} `json:"nodes"`
		}
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		err = tab.Call(ctx, "Accessibility.getFullAXTree", nil, &tree)
		cancel()
		if err != nil {
			t.Fatal(err)
		}
		checked := 0
		for _, n := range tree.Nodes {
			if n.Ignored || !axRoles[n.Role.Value] {
				continue
			}
			// The tree keeps the spaces around a label's words; the outline collapses them.
			collapsed := strings.Join(strings.Fields(n.Name.Value), " ")
			name, _ := json.Marshal(collapsed)
			line := "- " + n.Role.Value
			if collapsed != "" {
				line += " " + string(name)
			}
			if !strings.Contains(s.Text, line+" [ref=") {
				t.Errorf("%s: the accessibility tree has %s, the outline does not", path, line)
			}
			checked++
		}
		if checked == 0 {
			t.Errorf("%s: nothing compared", path)
		}
	}
}
