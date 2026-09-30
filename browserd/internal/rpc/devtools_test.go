//go:build unix

package rpc_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/page"
)

type logsReply struct {
	URL     string          `json:"url"`
	Entries []page.LogEntry `json:"entries"`
	Last    int64           `json:"last"`
	More    int             `json:"more"`
	Dropped bool            `json:"dropped"`
}

func (h *harness) logs(tab string, after int64, level string) logsReply {
	h.t.Helper()
	var r logsReply
	params := map[string]any{"tab_id": tab, "after": after}
	if level != "" {
		params["level"] = level
	}
	h.must("page.logs", params, &r)
	return r
}

// waitLogs reads the console until an entry holds want: an uncaught error arrives a moment after the
// load.
func (h *harness) waitLogs(tab, want string) logsReply {
	h.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		r := h.logs(tab, 0, "debug")
		for _, e := range r.Entries {
			if strings.Contains(e.Text, want) {
				return r
			}
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("no console entry holds %q: %+v", want, r.Entries)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func entry(r logsReply, source, text string) *page.LogEntry {
	for i := range r.Entries {
		if r.Entries[i].Source == source && strings.Contains(r.Entries[i].Text, text) {
			return &r.Entries[i]
		}
	}
	return nil
}

func TestConsoleAndPageErrors(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/devtools/console")
	tab := o.Tab.ID
	all := h.waitLogs(tab, "nobody caught this")
	h.waitLogs(tab, "Failed to load resource")
	all = h.logs(tab, 0, "debug")
	for _, c := range []struct{ source, text, level string }{
		{"console", `hello world {a: 1, b: "x"}`, "info"},
		{"console", "styled after", "info"},
		{"console", "careful", "warning"},
		{"console", "chatter", "debug"},
		{"exception", "Uncaught TypeError", "error"},
		{"exception", "Uncaught (in promise) Error: nobody caught this", "error"},
		{"network", "404", "error"},
	} {
		e := entry(all, c.source, c.text)
		if e == nil {
			t.Fatalf("no %s entry with %q: %+v", c.source, c.text, all.Entries)
		}
		if e.Level != c.level {
			t.Fatalf("%q is %s, want %s", c.text, e.Level, c.level)
		}
	}
	if e := entry(all, "console", "again"); e == nil || e.Count != 3 {
		t.Fatalf("a line logged three times: %+v", e)
	}
	if e := entry(all, "exception", "TypeError"); e == nil || !strings.Contains(e.URL, "/devtools/console") || e.Line == 0 {
		t.Fatalf("an uncaught error without where it was thrown: %+v", e)
	}
	if e := entry(all, "network", "404"); e == nil || !strings.HasSuffix(e.URL, "/devtools/missing.png") {
		t.Fatalf("a failed picture without its address: %+v", e)
	}
	// Logging an object shows its first level, and never runs the page's own getters to do it.
	if e := entry(all, "console", "(getter)"); e == nil {
		t.Fatalf("the trap object's getter is not shown as one: %+v", all.Entries)
	}
	if title := h.snapshot(tab).Title; title == "getter ran" {
		t.Fatal("reading the console ran a getter of the page's")
	}
	// The default level leaves the chatter out; "error" leaves out all but errors.
	if entry(h.logs(tab, 0, ""), "console", "chatter") != nil {
		t.Fatal("debug lines at the default level")
	}
	for _, e := range h.logs(tab, 0, "error").Entries {
		if e.Level != "error" {
			t.Fatalf("level error returned %+v", e)
		}
	}

	// Since the last read: nothing, until an action makes the page log; the action says how much.
	if r := h.logs(tab, all.Last, "debug"); len(r.Entries) != 0 {
		t.Fatalf("entries after the last one read: %+v", r.Entries)
	}
	s := h.snapshot(tab)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Break"), "element": "the break button"})
	logged, _ := r.Effects["logged"].(map[string]any)
	if logged == nil || logged["errors"] != float64(1) {
		t.Fatalf("the action's effects do not count its error: %+v", r.Effects)
	}
	after := h.logs(tab, all.Last, "")
	if len(after.Entries) != 1 || after.Entries[0].Text != "clicked and failed: 42" || after.Entries[0].Level != "error" {
		t.Fatalf("the entries since the last read: %+v", after.Entries)
	}
	// Paged: past limit the rest wait for the next call.
	var paged logsReply
	h.must("page.logs", map[string]any{"tab_id": tab, "level": "debug", "limit": 2}, &paged)
	if len(paged.Entries) != 2 || paged.More == 0 || paged.Last != paged.Entries[1].Seq {
		t.Fatalf("a page of two: %+v", paged)
	}
	if err := h.call("page.logs", map[string]any{"tab_id": tab, "level": "loud"}, nil); code(err) != -32602 {
		t.Fatalf("an unknown level: %v", err)
	}
}

type autoDialog struct {
	Seq     int64  `json:"seq"`
	Type    string `json:"type"`
	Message string `json:"message"`
}

func TestAlertsAndLeavingAreAnsweredForTheAgent(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/devtools/dialogs")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Save"), "element": "save"})
	if r.Effects["dialog"] != nil {
		t.Fatalf("an alert left open: %+v", r.Effects)
	}
	raw, _ := json.Marshal(r.Effects["dialogs_auto"])
	var auto []autoDialog
	_ = json.Unmarshal(raw, &auto)
	if len(auto) != 1 || auto[0].Type != "alert" || auto[0].Message != "Saved!" || auto[0].Seq == 0 {
		t.Fatalf("the alert the click met: %s", raw)
	}
	// The page ran on past its alert, and the action saw what it did.
	if !strings.Contains(r.Diff, "saved") {
		t.Fatalf("the diff after an answered alert: %q", r.Diff)
	}
	var published bool
	evs, _ := h.evlog.After(0, 20000)
	for _, e := range evs {
		if e.Type == "dialog.opened" {
			t.Fatalf("an answered alert published as open: %+v", e.Data)
		}
		if e.Type == "dialog.auto" {
			published = true
		}
	}
	if !published {
		t.Fatal("no dialog.auto event")
	}
	// A confirm is a decision, and stays the agent's.
	r = h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Delete"), "element": "delete"})
	if d, _ := r.Effects["dialog"].(map[string]any); d == nil || d["type"] != "confirm" {
		t.Fatalf("a confirm was not left to the agent: %+v", r.Effects)
	}
	h.must("dialog.answer", map[string]any{"tab_id": tab, "accept": false}, nil)
	// The page asks before it is left (the clicks gave it the user activation it needs): the agent's
	// own navigation goes on, and says it left.
	var nav struct {
		URL         string       `json:"url"`
		Status      int          `json:"status"`
		DialogsAuto []autoDialog `json:"dialogs_auto"`
	}
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + "/still"}, &nav)
	if !strings.HasSuffix(nav.URL, "/still") || nav.Status != 200 {
		t.Fatalf("the navigation away: %+v", nav)
	}
	if len(nav.DialogsAuto) != 1 || nav.DialogsAuto[0].Type != "beforeunload" {
		t.Fatalf("the question before leaving: %+v", nav.DialogsAuto)
	}
	if e := entry(h.logs(tab, 0, ""), "dialog", "alert (accepted by the browser): Saved!"); e == nil {
		t.Fatal("the answered alert is not in the console")
	}
	var gone struct {
		Status int `json:"status"`
	}
	h.must("page.navigate", map[string]any{"tab_id": tab, "url": h.site.URL + "/devtools/gone"}, &gone)
	if gone.Status != 404 {
		t.Fatalf("a missing page's status: %+v", gone)
	}
}

// TestAlertWaitsWhenTheAgentDoesNotHoldThePage: while the agent is paused for the operator (or a
// person drives), an alert is theirs to see, as any dialog.
func TestAlertWaitsWhenTheAgentDoesNotHoldThePage(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/devtools/dialogs")
	tab := o.Tab.ID
	save := refOf(t, h.snapshot(tab), "button", "Save")
	h.must("control.set", map[string]any{"group_id": o.Group.ID, "owner": "paused", "reason": "the operator looks"}, nil)
	operator := map[string]any{"actor": "operator"}
	r := h.mustAct(tab, map[string]any{"action": "click", "ref": save, "element": "save", "origin": operator})
	if d, _ := r.Effects["dialog"].(map[string]any); d == nil || d["type"] != "alert" {
		t.Fatalf("an alert while the agent is paused was answered for it: %+v", r.Effects)
	}
	h.must("dialog.answer", map[string]any{"tab_id": tab, "accept": true, "origin": operator}, nil)
}

type netReply struct {
	URL      string          `json:"url"`
	Requests []*page.Request `json:"requests"`
	Last     int64           `json:"last"`
	Skipped  int             `json:"skipped"`
}

func TestNetworkLogWithoutSecrets(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/devtools/app")
	tab := o.Tab.ID
	var first netReply
	h.must("page.network", map[string]any{"tab_id": tab}, &first)
	if len(first.Requests) == 0 || first.Requests[0].Type != "document" || first.Requests[0].Status != 200 {
		t.Fatalf("the page's own document: %+v", first.Requests)
	}
	s := h.snapshot(tab)
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Load"), "element": "load"})
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Sign in"), "element": "sign in"})
	var api netReply
	deadline := time.Now().Add(10 * time.Second)
	for {
		h.must("page.network", map[string]any{"tab_id": tab, "after": first.Last, "types": []string{"api"}}, &api)
		if len(api.Requests) == 2 && !api.Requests[0].Pending && !api.Requests[1].Pending {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the page's API calls: %+v", api.Requests)
		}
		time.Sleep(100 * time.Millisecond)
	}
	items, login := api.Requests[0], api.Requests[1]
	if items.Method != "GET" || items.Type != "fetch" || items.Status != 200 || items.MIME != "application/json" || !strings.Contains(items.Initiator, "script") {
		t.Fatalf("the items call: %+v", items)
	}
	if !strings.Contains(items.URL, "page=2&access_token=[withheld]") {
		t.Fatalf("the token in the address: %s", items.URL)
	}
	if login.Method != "POST" || !login.PostData {
		t.Fatalf("the sign-in call: %+v", login)
	}

	var detail page.RequestDetail
	h.must("page.request", map[string]any{"tab_id": tab, "id": items.ID, "body": true}, &detail)
	heads := map[string]string{}
	for _, hd := range append(detail.Request.RequestHeaders, detail.Request.ResponseHeaders...) {
		heads[hd.Name] = hd.Value
	}
	for _, name := range []string{"authorization", "x-api-key", "set-cookie"} {
		if heads[name] != page.Withheld {
			t.Fatalf("%s is %q", name, heads[name])
		}
	}
	if heads["x-trace"] != "t-1" || heads["x-request-id"] != "req-7" {
		t.Fatalf("ordinary headers lost: %+v", heads)
	}
	if detail.Body == nil {
		t.Fatalf("no body: %+v", detail)
	}
	want := `{"items":[{"name":"Blue mug","sku":"BM-1","code":"MUG"}],"session_token":"[withheld]","auth":{"refresh_token":"[withheld]"},"next":"/devtools/items?page=3"}`
	if *detail.Body != want {
		t.Fatalf("the body:\n%s\nwant\n%s", *detail.Body, want)
	}
	var loginDetail page.RequestDetail
	h.must("page.request", map[string]any{"tab_id": tab, "id": login.ID, "body": true}, &loginDetail)
	if loginDetail.Body == nil || !strings.Contains(*loginDetail.Body, `"token":"[withheld]"`) {
		t.Fatalf("the sign-in's answer: %+v", loginDetail)
	}
	// The picture of the first document is not text; a request the tab never had is not found.
	var doc page.RequestDetail
	h.must("page.request", map[string]any{"tab_id": tab, "id": first.Requests[0].ID, "body": true, "max_chars": 100}, &doc)
	if doc.Body == nil || !doc.Truncated {
		t.Fatalf("the document cut at 100 characters: %+v", doc)
	}
	if err := h.call("page.request", map[string]any{"tab_id": tab, "id": "r999"}, nil); code(err) != 1001 {
		t.Fatalf("an unknown request: %v", err)
	}
	// A socket is listed with its handshake's answer, its token cut like any address's.
	h.mustAct(tab, map[string]any{"action": "click", "ref": refOf(t, s, "button", "Live"), "element": "live"})
	var sockets netReply
	deadline = time.Now().Add(10 * time.Second)
	for {
		h.must("page.network", map[string]any{"tab_id": tab, "types": []string{"websocket"}}, &sockets)
		if len(sockets.Requests) == 1 && (sockets.Requests[0].Status != 0 || sockets.Requests[0].Failed != "") {
			break
		}
		if time.Now().After(deadline) {
			raw, _ := json.Marshal(sockets.Requests)
			t.Fatalf("the socket: %s", raw)
		}
		time.Sleep(100 * time.Millisecond)
	}
	if ws := sockets.Requests[0]; !strings.HasSuffix(ws.URL, "/devtools/socket?token=[withheld]") || !strings.HasPrefix(ws.URL, "ws://") {
		t.Fatalf("the socket's address: %s", ws.URL)
	}
	var failed netReply
	h.must("page.network", map[string]any{"tab_id": tab, "failed": true, "types": []string{"document", "fetch"}}, &failed)
	if len(failed.Requests) != 0 {
		raw, _ := json.Marshal(failed.Requests)
		t.Fatalf("failures where there were none: %s", raw)
	}

	// Nothing a credential was written as reaches the host, whatever it asks for.
	var everything json.RawMessage
	h.must("page.network", map[string]any{"tab_id": tab}, &everything)
	seen := string(everything)
	for _, r := range []string{items.ID, login.ID, first.Requests[0].ID} {
		var raw json.RawMessage
		h.must("page.request", map[string]any{"tab_id": tab, "id": r, "body": true}, &raw)
		seen += string(raw)
	}
	var logs json.RawMessage
	h.must("page.logs", map[string]any{"tab_id": tab, "level": "debug"}, &logs)
	seen += string(logs)
	if strings.Contains(seen, "SECRET-") {
		i := strings.Index(seen, "SECRET-")
		t.Fatalf("a secret reached the host: …%s…", seen[max(0, i-80):min(len(seen), i+40)])
	}
}

func TestInspectSaysWhyAnElementDoesNotShow(t *testing.T) {
	h := start(t, nil)
	o := h.open("g1", "project-a", "/devtools/inspect")
	tab := o.Tab.ID
	s := h.snapshot(tab)
	inspect := func(params map[string]any) map[string]any {
		t.Helper()
		params["tab_id"] = tab
		var r map[string]any
		h.must("page.inspect", params, &r)
		return r
	}
	reasons := func(r map[string]any) string {
		raw, _ := json.Marshal(r["reasons"])
		return string(raw)
	}
	for _, c := range []struct {
		params             map[string]any
		visible, clickable bool
		reason             string
	}{
		{map[string]any{"selector": "#inpanel"}, false, false, `display: none on div#panel`},
		{map[string]any{"ref": refOf(t, s, "button", "Ghost")}, false, false, "opacity 0 on itself"},
		{map[string]any{"ref": refOf(t, s, "button", "Under a veil")}, true, false, `covered by div#veil`},
		{map[string]any{"selector": "#away"}, false, false, "placed outside the page"},
		{map[string]any{"selector": "#box button"}, false, false, "cut off by div#box"},
		// Out of its pane's view, but an action scrolls it in: clickable, with the way said.
		{map[string]any{"selector": "#deep"}, true, true, "scrolling pane div#pane"},
		{map[string]any{"selector": "#off"}, true, false, "disabled"},
		{map[string]any{"selector": "#nothru"}, true, false, "pointer-events: none"},
	} {
		r := inspect(c.params)
		if r["visible"] != c.visible || r["clickable"] != c.clickable || !strings.Contains(reasons(r), c.reason) {
			t.Fatalf("%v: visible %v, clickable %v, reasons %s; want %v, %v and %q", c.params, r["visible"], r["clickable"], reasons(r), c.visible, c.clickable, c.reason)
		}
	}
	fine := inspect(map[string]any{"ref": refOf(t, s, "button", "Fine")})
	if fine["visible"] != true || fine["clickable"] != true || reasons(fine) != "[]" {
		t.Fatalf("a plain button: %+v", fine)
	}
	styles, _ := fine["styles"].(map[string]any)
	if styles["color"] != "rgb(1, 2, 3)" || styles["display"] == nil {
		t.Fatalf("the button's styles: %+v", styles)
	}
	if html, _ := fine["html"].(string); !strings.HasPrefix(html, "<button") || strings.Contains(html, "onclick") {
		t.Fatalf("the button's markup: %s", html)
	}
	form := inspect(map[string]any{"selector": "form", "max_chars": 2000})
	html, _ := form["html"].(string)
	if strings.Contains(html, "SECRET-") || strings.Contains(html, "steal()") || !strings.Contains(html, `value="shoes"`) {
		t.Fatalf("the form's markup: %s", html)
	}
	if form["matches"] != float64(1) {
		t.Fatalf("matches: %v", form["matches"])
	}
	// The element found by its selector has a ref now, which an action takes.
	ref, _ := inspect(map[string]any{"selector": "#under"})["ref"].(string)
	if ref == "" {
		t.Fatal("no ref for an element found by its selector")
	}
	if _, err := h.act(tab, map[string]any{"action": "click", "ref": ref, "element": "under"}); code(err) != 1004 {
		t.Fatalf("clicking the covered button: %v", err)
	}
	if err := h.call("page.inspect", map[string]any{"tab_id": tab, "selector": "#nothing-here"}, nil); code(err) != 1001 {
		t.Fatalf("a selector that matches nothing: %v", err)
	}
	if err := h.call("page.inspect", map[string]any{"tab_id": tab, "selector": "[[["}, nil); code(err) != -32602 {
		t.Fatalf("a selector that is not one: %v", err)
	}
	if err := h.call("page.inspect", map[string]any{"tab_id": tab}, nil); code(err) != -32602 {
		t.Fatalf("neither a ref nor a selector: %v", err)
	}
}
