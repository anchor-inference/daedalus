package rpc_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
)

type cookieRow struct {
	Name     string  `json:"name"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	Secure   bool    `json:"secure"`
	HTTPOnly bool    `json:"http_only"`
	SameSite string  `json:"same_site"`
}

func listCookies(h *harness, group, domain string) ([]cookieRow, string) {
	h.t.Helper()
	var raw json.RawMessage
	params := map[string]any{"group_id": group}
	if domain != "" {
		params["domain"] = domain
	}
	h.must("cookies.list", params, &raw)
	var r struct {
		Cookies []cookieRow `json:"cookies"`
	}
	if err := json.Unmarshal(raw, &r); err != nil {
		h.t.Fatal(err)
	}
	return r.Cookies, string(raw)
}

func TestCookiesSetReachThePageAndNeverComeBack(t *testing.T) {
	launchOnly(t, "cookies are never imported into the operator's Chrome (TestAttachRefusals)")
	needChromium(t)
	h := start(t, nil)
	o := h.open("c1", "cookie-profile", "")
	later := float64(time.Now().Add(48 * time.Hour).Unix())
	cookies := []map[string]any{
		// Host-only, by address, as an export marks a cookie of one host.
		{"name": "sid", "value": "VALUE-host-only-1", "url": h.site.URL + "/", "path": "/", "httpOnly": true, "expires": later},
		// A session cookie: no expiry.
		{"name": "pref", "value": "VALUE-session-2", "url": h.site.URL + "/"},
		// A domain cookie of another site, with every flag.
		{"name": "auth", "value": "VALUE-domain-3", "domain": ".shop.example", "path": "/", "secure": true, "sameSite": "Lax", "expires": later},
		// Refused here, and named: no domain and no address.
		{"name": "lost", "value": "VALUE-refused-4"},
		{"name": "odd", "value": "VALUE-refused-5", "domain": ".shop.example", "sameSite": "Sometimes"},
	}
	var raw json.RawMessage
	h.must("cookies.set", map[string]any{"group_id": "c1", "cookies": cookies}, &raw)
	var set struct {
		Set      int `json:"set"`
		Rejected []struct {
			Name   string `json:"name"`
			Reason string `json:"reason"`
		} `json:"rejected"`
	}
	if err := json.Unmarshal(raw, &set); err != nil {
		t.Fatal(err)
	}
	if set.Set != 3 || len(set.Rejected) != 2 || set.Rejected[0].Name != "lost" || set.Rejected[1].Name != "odd" {
		t.Fatalf("set: %s", raw)
	}
	if strings.Contains(string(raw), "VALUE-") {
		t.Fatalf("a value came back from cookies.set: %s", raw)
	}

	// The page is sent them, as a login made by hand would be.
	var nav browser.NavResult
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/cookie-names"}, &nav)
	if nav.Title != "pref,sid" && nav.Title != "sid,pref" {
		t.Fatalf("the page was sent %q", nav.Title)
	}

	rows, listed := listCookies(h, "c1", "")
	if strings.Contains(listed, "VALUE-") || strings.Contains(listed, `"value"`) {
		t.Fatalf("cookies.list told a value: %s", listed)
	}
	if len(rows) != 3 {
		t.Fatalf("listed: %s", listed)
	}
	byName := map[string]cookieRow{}
	for _, r := range rows {
		byName[r.Name] = r
	}
	if r := byName["auth"]; r.Domain != ".shop.example" || !r.Secure || r.SameSite != "Lax" || r.Expires == 0 {
		t.Fatalf("auth: %+v", r)
	}
	if r := byName["sid"]; !r.HTTPOnly || r.Expires == 0 || strings.HasPrefix(r.Domain, ".") {
		t.Fatalf("sid: %+v", r)
	}
	if r := byName["pref"]; r.Expires != 0 {
		t.Fatalf("a session cookie has an expiry: %+v", r)
	}
	if only, _ := listCookies(h, "c1", "shop.example"); len(only) != 1 || only[0].Name != "auth" {
		t.Fatalf("by domain: %+v", only)
	}

	// Cleared for one site, the others stay.
	var cleared struct {
		Cleared int `json:"cleared"`
	}
	h.must("cookies.clear", map[string]any{"group_id": "c1", "domain": "shop.example"}, &cleared)
	if cleared.Cleared != 1 {
		t.Fatalf("cleared: %+v", cleared)
	}
	if rows, _ := listCookies(h, "c1", ""); len(rows) != 2 {
		t.Fatalf("after clearing one site: %+v", rows)
	}
	if err := h.call("cookies.clear", map[string]any{"group_id": "c1", "domain": " . "}, nil); code(err) != -32602 {
		t.Fatalf("clearing every site at once: %v", err)
	}

	// The profile keeps them: closed and started again, the persistent cookie is there and the
	// session cookie went with the browser, as Chromium keeps a jar.
	var list struct {
		Browsers []struct {
			ID      string `json:"id"`
			Profile string `json:"profile"`
		} `json:"browsers"`
	}
	h.must("browser.list", map[string]any{}, &list)
	for _, b := range list.Browsers {
		if b.Profile == "cookie-profile" {
			h.must("browser.close", map[string]any{"browser_id": b.ID}, nil)
		}
	}
	h.open("c2", "cookie-profile", "")
	rows, _ = listCookies(h, "c2", "")
	if len(rows) != 1 || rows[0].Name != "sid" {
		t.Fatalf("after a restart the profile holds %+v", rows)
	}
}

func TestThrowawayGroupsKeepTheirOwnCookies(t *testing.T) {
	launchOnly(t, "the operator's Chrome takes no throwaway contexts (TestAttachRefusals)")
	needChromium(t)
	h := start(t, nil)
	h.open("e1", browser.EphemeralProfile, "")
	h.open("e2", browser.EphemeralProfile, "")
	h.must("cookies.set", map[string]any{"group_id": "e1", "cookies": []map[string]any{{"name": "mine", "value": "v", "domain": ".shop.example"}}}, nil)
	if rows, _ := listCookies(h, "e1", ""); len(rows) != 1 {
		t.Fatalf("e1: %+v", rows)
	}
	if rows, _ := listCookies(h, "e2", ""); len(rows) != 0 {
		t.Fatalf("a throwaway group sees another's cookies: %+v", rows)
	}
	if err := h.call("cookies.set", map[string]any{"group_id": "e1", "cookies": []map[string]any{}}, nil); code(err) != -32602 {
		t.Fatalf("an empty list: %v", err)
	}
	if err := h.call("cookies.list", map[string]any{"group_id": "nope"}, nil); code(err) != 1001 {
		t.Fatalf("no such group: %v", err)
	}
}
