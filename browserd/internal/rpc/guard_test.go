package rpc_test

import (
	"context"
	"net"
	"net/http/httptest"
	"net/netip"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/netwall"
)

// publicNames answers two names with addresses the wall judges as the internet; nothing is dialled
// there, the redirect serves them from the fixture.
type publicNames map[string]string

func (p publicNames) LookupNetIP(_ context.Context, _, host string) ([]netip.Addr, error) {
	if a, ok := p[host]; ok {
		return []netip.Addr{netip.MustParseAddr(a)}, nil
	}
	return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
}

func (h *harness) blockedNavigations() []map[string]any {
	evs, _ := h.evlog.After(0, 20000)
	var out []map[string]any
	for _, e := range evs {
		if e.Type == "navigation.blocked" {
			if m, ok := e.Data.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}

var linkRef = regexp.MustCompile(`link "onward" \[ref=(e\d+)\]`)

// With an allowlist, a page cannot take the agent off it: a link the agent clicks and a redirect of
// an allowed page are stopped and published, while the allowlisted host loads. Without one, the same
// link is followed.
func TestAPagesOwnNavigationsMeetTheAllowlist(t *testing.T) {
	needChromium(t)
	h := startWall(t, nil, nil, func(site *httptest.Server, publish func(netwall.Egress)) *netwall.Browsers {
		w := netwall.New(netwall.Options{Events: publish, Resolver: publicNames{"allowed.example": "93.184.216.34", "offlist.example": "93.184.216.35"}})
		fixtureAddr := netip.MustParseAddrPort(site.Listener.Addr().String())
		w.SetTestRedirect(func(netip.AddrPort) netip.AddrPort { return fixtureAddr })
		return netwall.NewBrowsers(w)
	})
	rules := map[string]any{"services_ports": [][2]int{}, "local_sites": "services", "lan_allow": []string{}, "egress_allow": []string{"allowed.example"}}
	h.must("net.configure", rules, nil)
	o := h.open("g1", "project-a", "")
	var nav struct {
		URL   string `json:"url"`
		Title string `json:"title"`
		Error string `json:"error"`
	}
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://allowed.example/still"}, &nav)
	if nav.Title != "Still" {
		t.Fatalf("the allowlisted host: %+v", nav)
	}
	if data := blocked(h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://offlist.example/still"}, nil)); data == nil || data["reason"] != "egress_allow" {
		t.Fatalf("the agent's own navigation off the list: %v", data)
	}

	click := func() {
		t.Helper()
		var snap struct {
			Text string `json:"text"`
		}
		h.must("page.snapshot", map[string]any{"tab_id": o.Tab.ID}, &snap)
		m := linkRef.FindStringSubmatch(snap.Text)
		if m == nil {
			t.Fatalf("no link in %q", snap.Text)
		}
		_ = h.call("page.act", map[string]any{"tab_id": o.Tab.ID, "action": "click", "ref": m[1], "element": "the onward link"}, nil)
	}
	// A stopped navigation leaves Chromium's own error page at the address it refused, so what shows
	// whether the page left is what loaded: the fixture's Still page has that title.
	arrived := func() bool {
		var tabs struct {
			Tabs []struct {
				URL   string `json:"url"`
				Title string `json:"title"`
			} `json:"tabs"`
		}
		h.must("tab.list", map[string]any{"group_id": "g1"}, &tabs)
		return strings.Contains(tabs.Tabs[0].URL, "offlist") && tabs.Tabs[0].Title == "Still"
	}

	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://allowed.example/link?to=http://offlist.example/still"}, nil)
	click()
	time.Sleep(time.Second)
	if arrived() {
		t.Fatal("a link took the page off the allowlist")
	}
	got := h.blockedNavigations()
	if len(got) != 1 || !strings.HasPrefix(got[0]["url"].(string), "http://offlist.example/") || got[0]["reason"] != "egress_allow" || got[0]["by"] != "page" {
		t.Fatalf("the stopped link was published as %v", got)
	}

	// A redirect of an allowed page is the page's navigation too.
	_ = h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://allowed.example/go?to=http://offlist.example/still"}, &nav)
	if arrived() {
		t.Fatalf("a redirect took the page off the allowlist (%+v)", nav)
	}
	if n := len(h.blockedNavigations()); n != 2 {
		t.Fatalf("the redirect was not published: %d stopped", n)
	}

	// No allowlist, no guard: the same link is followed.
	delete(rules, "egress_allow")
	h.must("net.configure", rules, nil)
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://allowed.example/link?to=http://offlist.example/still"}, nil)
	click()
	deadline := time.Now().Add(10 * time.Second)
	for !arrived() {
		if time.Now().After(deadline) {
			t.Fatal("without an allowlist the link was not followed")
		}
		time.Sleep(100 * time.Millisecond)
	}
}
