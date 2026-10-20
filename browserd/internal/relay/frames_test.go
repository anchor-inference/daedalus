package relay

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

type golden struct {
	Proto  int    `json:"proto"`
	About  string `json:"about"`
	Frames []struct {
		Name    string          `json:"name"`
		From    string          `json:"from"`
		ValueOf string          `json:"value_of"`
		Frame   json.RawMessage `json:"frame"`
	} `json:"frames"`
}

func readGolden(t *testing.T) golden {
	t.Helper()
	b, err := os.ReadFile("testdata/frames.json")
	if err != nil {
		t.Fatal(err)
	}
	var g golden
	if err := json.Unmarshal(b, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

func strict(t *testing.T, name string, raw []byte, v any) {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		t.Fatalf("%s: %v in %s", name, err, raw)
	}
}

func sameJSON(t *testing.T, name string, a, b []byte) {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(x, y) {
		t.Fatalf("%s: encoded back as\n%s\nnot\n%s", name, b, a)
	}
}

// Every golden frame decodes strictly into the daemon's type for it and encodes back as it was, so a
// field the extension sends that the daemon does not know, or one the daemon drops, fails here; the
// arguments of each call and the value of each result are held to their own types the same way.
func TestGoldenFrames(t *testing.T) {
	g := readGolden(t)
	if g.Proto != Proto {
		t.Fatalf("the golden frames are of protocol %d, the daemon's %d", g.Proto, Proto)
	}
	types := map[string]bool{}
	ops := map[string]bool{}
	tabOps := map[string]bool{}
	byes := map[string]bool{}
	for _, f := range g.Frames {
		typ, err := Type(f.Frame)
		if err != nil {
			t.Fatalf("%s: %v", f.Name, err)
		}
		types[typ] = true
		var v any
		switch typ {
		case THello:
			v = &Hello{}
		case TWelcome:
			v = &Welcome{}
		case TCDP:
			v = &CDP{}
		case TCall:
			v = &Call{}
		case TResult:
			v = &Result{}
		case TEvent:
			v = &Event{}
		case TTabs:
			v = &Tabs{}
		case TInput:
			v = &Input{}
		case TPing, TPong, TCheck:
			v = &Ping{}
		case TCheckOK:
			v = &CheckOK{}
		case TBye:
			v = &Bye{}
		default:
			t.Fatalf("%s: a frame of type %q the daemon has no type for", f.Name, typ)
		}
		strict(t, f.Name, f.Frame, v)
		back, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		sameJSON(t, f.Name, f.Frame, back)
		switch x := v.(type) {
		case *Call:
			ops[x.Op] = true
			raw, _ := json.Marshal(x.Args)
			strict(t, f.Name+" args", raw, argsOf(t, x.Op))
		case *Result:
			if f.ValueOf != "" {
				if into := valueOf(f.ValueOf); into != nil {
					strict(t, f.Name+" value", x.Value, into)
				}
			}
		case *Tabs:
			tabOps[x.Op] = true
		case *Bye:
			byes[x.Reason] = true
		}
	}
	for _, typ := range []string{THello, TResult, TEvent, TTabs, TInput, TPong, TCheck, TWelcome, TCDP, TCall, TPing, TCheckOK, TBye} {
		if !types[typ] {
			t.Errorf("no golden frame of type %s", typ)
		}
	}
	for _, op := range []string{OpWindowEnsure, OpTabCreate, OpTabClose, OpTabActivate, OpWindowBounds, OpDownloadsSearch, OpCookiesNames, OpGroupClose, OpNotify, OpPanelGrant, OpPanelEvent} {
		if !ops[op] {
			t.Errorf("no golden call of %s", op)
		}
	}
	for _, op := range []string{TabsCreated, TabsRemoved, TabsUpdated, TabsJoined, TabsLeft, TabsGroupClosed} {
		if !tabOps[op] {
			t.Errorf("no golden tabs frame of %s", op)
		}
	}
	for _, r := range []string{ByeShutdown, ByeReplaced, ByeAnotherRelay, ByeProto, ByeLost, ByeBadFrame, "not_running", "unavailable"} {
		if !byes[r] {
			t.Errorf("no golden bye of %s", r)
		}
	}
}

func argsOf(t *testing.T, op string) any {
	switch op {
	case OpWindowEnsure, OpGroupClose:
		return &GroupArgs{}
	case OpTabCreate:
		return &TabCreateArgs{}
	case OpTabClose, OpTabActivate:
		return &TabArgs{}
	case OpWindowBounds:
		return &WindowBoundsArgs{}
	case OpDownloadsSearch:
		return &DownloadsSearchArgs{}
	case OpCookiesNames:
		return &CookiesNamesArgs{}
	case OpNotify:
		return &NotifyArgs{}
	case OpPanelGrant:
		return &PanelGrantArgs{}
	case OpPanelEvent:
		return &PanelEventArgs{}
	}
	t.Fatalf("a call of %q, which is no operation", op)
	return nil
}

func valueOf(op string) any {
	switch op {
	case OpWindowEnsure:
		return &WindowEnsured{}
	case OpTabCreate:
		return &TabCreated{}
	case OpWindowBounds:
		return &WindowBounds{}
	case OpDownloadsSearch:
		return &Download{}
	case OpCookiesNames:
		return &CookieNames{}
	case OpNotify, OpPanelGrant, OpPanelEvent:
		return &struct{}{}
	}
	return nil // a DevTools method's value, Chrome's own
}

func TestPieces(t *testing.T) {
	big := bytes.Repeat([]byte("abcdefghij"), 250_000) // 2.5 MB, a screenshot's result
	pieces := Split(big, 1<<20)
	if len(pieces) != 3 {
		t.Fatalf("%d pieces", len(pieces))
	}
	for i, p := range pieces {
		if len(p) > 1<<20 {
			t.Fatalf("piece %d is %d bytes", i, len(p))
		}
		want := PieceMore
		if i == len(pieces)-1 {
			want = PieceLast
		}
		if p[0] != want {
			t.Fatalf("piece %d marked %d", i, p[0])
		}
	}
	j := Joiner{Max: MaxFromExtension}
	var got []byte
	for i, p := range pieces {
		out, err := j.Add(p)
		if err != nil {
			t.Fatal(err)
		}
		if (out != nil) != (i == len(pieces)-1) {
			t.Fatalf("piece %d: whole frame %v", i, out != nil)
		}
		got = out
	}
	if !bytes.Equal(got, big) {
		t.Fatal("joined frame differs")
	}
	// A frame that fits is one piece, and a frame of exactly the room left is still one.
	if p := Split([]byte(`{"t":"pong","n":1}`), 1<<20); len(p) != 1 || p[0][0] != PieceLast {
		t.Fatalf("small frame: %q", p)
	}
	if p := Split(bytes.Repeat([]byte("x"), 99), 100); len(p) != 1 {
		t.Fatalf("a frame of the room: %d pieces", len(p))
	}
	if _, err := j.Add([]byte{7, '{'}); err == nil {
		t.Fatal("a piece with a bad mark was taken")
	}
	if _, err := j.Add(nil); err == nil {
		t.Fatal("an empty piece was taken")
	}
	small := Joiner{Max: 10}
	if _, err := small.Add(append([]byte{PieceMore}, bytes.Repeat([]byte("x"), 8)...)); err != nil {
		t.Fatal(err)
	}
	if _, err := small.Add(append([]byte{PieceLast}, bytes.Repeat([]byte("x"), 8)...)); err != ErrTooLarge {
		t.Fatalf("a frame past the bound: %v", err)
	}
}

func TestAllowed(t *testing.T) {
	for _, m := range []string{"Page.navigate", "Runtime.evaluate", "DOM.describeNode", "DOMSnapshot.captureSnapshot", "Input.dispatchMouseEvent",
		"Accessibility.getFullAXTree", "Log.enable", "Fetch.enable", "Network.getResponseBody", "Emulation.setFocusEmulationEnabled",
		"Emulation.setDeviceMetricsOverride", "Target.setAutoAttach", "Target.detachFromTarget", "DOMDebugger.getEventListeners"} {
		if err := Allowed(m); err != nil {
			t.Errorf("%s refused: %v", m, err)
		}
	}
	for _, m := range []string{"Network.getCookies", "Network.getAllCookies", "Network.setCookie", "Network.setCookies", "Network.deleteCookies",
		"Network.clearBrowserCookies", "Network.clearBrowserCache", "Storage.getCookies", "Storage.clearDataForOrigin", "Browser.getVersion",
		"Browser.setDownloadBehavior", "Page.setDownloadBehavior", "Target.createTarget", "Target.closeTarget", "Target.getTargets",
		"Target.attachToTarget", "Target.createBrowserContext", "Emulation.setUserAgentOverride", "Emulation.setGeolocationOverride",
		"Security.setIgnoreCertificateErrors", "SystemInfo.getInfo", "nonsense", ".x"} {
		if Allowed(m) == nil {
			t.Errorf("%s carried", m)
		}
	}
}

func TestParseSession(t *testing.T) {
	for s, want := range map[string]struct {
		tab   int
		child string
		ok    bool
	}{
		"x412": {412, "", true}, "x412/ABC": {412, "ABC", true}, "x0": {0, "", true},
		"412": {0, "", false}, "x": {0, "", false}, "x-1": {0, "", false}, "x01": {0, "", false}, "x4/": {0, "", false},
		"xab": {0, "", false}, "": {0, "", false},
	} {
		tab, child, ok := ParseSession(s)
		if ok != want.ok || (ok && (tab != want.tab || child != want.child)) {
			t.Errorf("%q: %d %q %v", s, tab, child, ok)
		}
	}
	if SessionOf(7) != "x7" {
		t.Fatal(SessionOf(7))
	}
}
