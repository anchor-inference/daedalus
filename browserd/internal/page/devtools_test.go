package page

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/cdp"
)

func TestSecretNames(t *testing.T) {
	for name, want := range map[string]bool{
		"access_token": true, "apiKey": true, "X-Api-Key": true, "authorization": true, "sessionid": true, "PHPSESSID": false,
		"csrf_token": true, "X-Amz-Signature": true, "code": true, "key": true, "page": false, "q": false, "sort": false,
		"keyword": false, "monkey": false, "author": false, "passenger": false,
	} {
		if got := SecretName(name); got != want {
			t.Errorf("SecretName(%q) = %v", name, got)
		}
	}
	// In a body the ordinary fields of an API stay: an error's code, an issue's key, a commit's hash.
	for name, want := range map[string]bool{
		"session_token": true, "refresh_token": true, "password": true, "api_key": true, "accessKey": true, "private_key": true,
		"client_secret": true, "code": false, "key": false, "hash": false, "sku": false, "sort_key": false, "authorName": false,
	} {
		if got := SecretMember(name); got != want {
			t.Errorf("SecretMember(%q) = %v", name, got)
		}
	}
}

func TestRedactURL(t *testing.T) {
	for in, want := range map[string]string{
		"https://api.example/v1/items?page=2&api_key=abc123&q=mugs":       "https://api.example/v1/items?page=2&api_key=[withheld]&q=mugs",
		"https://someone:hunter2@example.com/x":                           "https://example.com/x",
		"https://app.example/cb#access_token=tok&token_type=bearer":       "https://app.example/cb#access_token=[withheld]&token_type=[withheld]",
		"https://x.example/s?X-Amz-Signature=ff&X-Amz-Date=1":             "https://x.example/s?X-Amz-Signature=[withheld]&X-Amz-Date=1",
		"https://x.example/p?id=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig": "https://x.example/p?id=[withheld]",
		"https://x.example/plain?page=1#section-2":                        "https://x.example/plain?page=1#section-2",
	} {
		if got := RedactURL(in); got != want {
			t.Errorf("RedactURL(%q)\n = %q\nwant %q", in, got, want)
		}
	}
	if got := RedactURL("https://x.example/" + strings.Repeat("a", 5000)); len(got) > urlMax+len("…") {
		t.Errorf("a long address kept at %d bytes", len(got))
	}
}

func TestRedactHeaders(t *testing.T) {
	for _, c := range [][3]string{
		{"Authorization", "Bearer abc", Withheld}, {"Cookie", "sid=1", Withheld}, {"Set-Cookie", "sid=1; HttpOnly", Withheld},
		{"X-CSRF-Token", "t", Withheld}, {"X-Goog-Api-Key", "k", Withheld}, {"Content-Type", "application/json", "application/json"},
		{"X-Debug", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc", Withheld},
	} {
		if got := RedactHeader(c[0], c[1]); got != c[2] {
			t.Errorf("RedactHeader(%q, %q) = %q", c[0], c[1], got)
		}
	}
}

func TestRedactBody(t *testing.T) {
	for _, c := range []struct{ mime, in, want string }{
		{"application/json",
			`{"b":1,"a":{"password":"p","list":[{"token":"t"},{"name":"n"}]},"api_key":{"nested":true},"code":"E42","n":null}`,
			`{"b":1,"a":{"password":"[withheld]","list":[{"token":"[withheld]"},{"name":"n"}]},"api_key":"[withheld]","code":"E42","n":null}`},
		{"application/vnd.api+json", `[1, 2.50, "x"]`, `[1,2.50,"x"]`},
		{"application/json", `{"broken": `, `{"broken": `},
		{"application/x-www-form-urlencoded", "user=someone&password=pw&next=%2F", "user=someone&password=[withheld]&next=%2F"},
		{"text/html", `<input type="hidden" name="authenticity_token" value="abc"><input name="q" value="shoes"><meta name="csrf-token" content="xyz">`,
			`<input type="hidden" name="authenticity_token" value="[withheld]"><input name="q" value="shoes"><meta name="csrf-token" content="[withheld]">`},
		{"application/javascript", `fetch(u, {headers: {"Authorization": "Bearer abc", "X-Trace": "t-1"}, body: "user=a&password=bcde"})`,
			`fetch(u, {headers: {"Authorization": "[withheld]", "X-Trace": "t-1"}, body: "user=a&password=[withheld]"})`},
		{"text/plain", "session_token = 'abcd efgh' and refresh_token: zzzz9", "session_token = '[withheld]' and refresh_token: [withheld]"},
	} {
		if got := RedactBody(c.in, c.mime); got != c.want {
			t.Errorf("RedactBody(%s, %q)\n = %q\nwant %q", c.mime, c.in, got, c.want)
		}
	}
}

func obj(t *testing.T, s string) remoteObject {
	t.Helper()
	var o remoteObject
	if err := json.Unmarshal([]byte(s), &o); err != nil {
		t.Fatal(err)
	}
	return o
}

func TestConsoleText(t *testing.T) {
	str := func(s string) remoteObject { b, _ := json.Marshal(s); return remoteObject{Type: "string", Value: b} }
	for _, c := range []struct {
		args []remoteObject
		want string
	}{
		{[]remoteObject{str("hello %s, %d items"), str("you"), obj(t, `{"type":"number","value":3}`)}, "hello you, 3 items"},
		{[]remoteObject{str("%cred"), str("color: red"), str("after")}, "red after"},
		{[]remoteObject{str("100%% sure")}, "100% sure"},
		{[]remoteObject{obj(t, `{"type":"object","className":"Object","description":"Object","preview":{"overflow":true,"properties":[{"name":"a","type":"number","value":"1"},{"name":"b","type":"string","value":"x"},{"name":"g","type":"accessor"}]}}`)},
			`{a: 1, b: "x", g: (getter), …}`},
		{[]remoteObject{obj(t, `{"type":"object","subtype":"array","description":"Array(2)","preview":{"properties":[{"name":"0","type":"number","value":"1"},{"name":"1","type":"object","value":"Object"}]}}`)},
			"Array(2) [1, Object]"},
		{[]remoteObject{obj(t, `{"type":"object","subtype":"error","description":"TypeError: x is not a function\n    at a (app.js:1:2)\n    at b (app.js:3:4)\n    at c (app.js:5:6)\n    at d (app.js:7:8)"}`)},
			"TypeError: x is not a function\n  at a (app.js:1:2)\n  at b (app.js:3:4)\n  at c (app.js:5:6)"},
		{[]remoteObject{obj(t, `{"type":"undefined"}`), obj(t, `{"type":"object","subtype":"null","value":null}`), obj(t, `{"type":"number","unserializableValue":"NaN"}`)}, "undefined null NaN"},
	} {
		if got := consoleText(c.args); got != c.want {
			t.Errorf("consoleText = %q, want %q", got, c.want)
		}
	}
}

func consoleEvent(t *testing.T, kind string, args ...string) cdp.Event {
	t.Helper()
	parts := make([]json.RawMessage, len(args))
	for i, a := range args {
		b, _ := json.Marshal(map[string]any{"type": "string", "value": a})
		parts[i] = b
	}
	raw, _ := json.Marshal(map[string]any{"type": kind, "args": parts, "timestamp": 1.79e12})
	return cdp.Event{Method: "Runtime.consoleAPICalled", Params: raw}
}

func TestConsoleRingFoldsAndPages(t *testing.T) {
	p := &Model{}
	tab := &browser.Tab{}
	mark := p.Mark(tab)
	c := p.consoleOf(tab, true)
	c.release = true // no release call on a tab without a browser
	for i := 0; i < 3; i++ {
		p.logEvent(tab, consoleEvent(t, "log", "same"))
	}
	p.logEvent(tab, consoleEvent(t, "error", "bad"))
	p.logEvent(tab, consoleEvent(t, "warning", "careful"))
	r, err := p.Logs(tab, 0, "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Entries) != 3 || r.Entries[0].Count != 3 || r.Entries[1].Level != "error" || r.Last != 5 {
		t.Fatalf("folded: %+v", r)
	}
	if errs, warns := p.LogCounts(tab, mark); errs != 1 || warns != 1 {
		t.Fatalf("counts %d, %d", errs, warns)
	}
	// A line folded after the reader's place comes again, with its new number.
	p.logEvent(tab, consoleEvent(t, "warning", "careful"))
	r, _ = p.Logs(tab, r.Last, "", 0)
	if len(r.Entries) != 1 || r.Entries[0].Count != 2 {
		t.Fatalf("after the last read: %+v", r.Entries)
	}
	for i := 0; i < logsKept+10; i++ {
		p.logEvent(tab, consoleEvent(t, "log", "line "+strings.Repeat("x", i%7)+string(rune('a'+i%26))))
	}
	r, _ = p.Logs(tab, 6, "", LogsMax)
	if !r.Dropped || len(r.Entries) != LogsMax {
		t.Fatalf("a full ring read from an old place: dropped %v, %d entries", r.Dropped, len(r.Entries))
	}
	// A place past the end is a daemon that started again: read from the start.
	if r, _ := p.Logs(tab, 1<<40, "", 1); len(r.Entries) != 1 || r.More == 0 {
		t.Fatalf("a place past the end: %+v", r)
	}
	long := strings.Repeat("é", logTextMax*2)
	p.logEvent(tab, consoleEvent(t, "log", long))
	r, _ = p.Logs(tab, r.Last, "", LogsMax)
	if got := []rune(r.Entries[len(r.Entries)-1].Text); len(got) != logTextMax {
		t.Fatalf("a long line kept at %d characters", len(got))
	}
}
