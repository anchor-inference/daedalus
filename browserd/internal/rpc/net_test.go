package rpc_test

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	protowire "github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// blocked is the 1102 error's data, or nil when err is not one.
func blocked(err error) map[string]any {
	var we *protowire.Error
	if errors.As(err, &we) && we.Code == 1102 {
		data, _ := we.Data.(map[string]any)
		return data
	}
	return nil
}

// The wall through the daemon's own methods: a navigation it refuses is the agent's 1102 with the
// reason, the operator's grant opens an asked port for the group's browser, and the rules are read
// strictly.
func TestTheNetworkWallThroughTheDaemon(t *testing.T) {
	needChromium(t)
	h := start(t, nil)
	var reached int
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached++
		fmt.Fprint(w, "<title>the operator's own app</title>")
	}))
	defer other.Close()
	otherPort := other.Listener.Addr().(*net.TCPAddr).Port
	sitePort := h.site.Listener.Addr().(*net.TCPAddr).Port
	o := h.open("g1", "project-a", "/still")

	for _, c := range []struct {
		url, decision, reason string
	}{
		{other.URL + "/", "deny", "loopback"},
		{"http://169.254.169.254/latest/meta-data/", "deny", "metadata"},
		{"http://" + lanAddress(t) + "/", "deny", "private"},
		{"data:text/html,x", "", ""}, // refused by the scheme check before the wall: 1004
	} {
		err := h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": c.url}, nil)
		if c.decision == "" {
			if code(err) != 1004 {
				t.Errorf("%s: %v", c.url, err)
			}
			continue
		}
		data := blocked(err)
		if data == nil || data["decision"] != c.decision || data["reason"] != c.reason {
			t.Errorf("%s: %v (data %v)", c.url, err, data)
		}
	}

	// Natively the operator's other local ports are an ask; a grant opens exactly the one asked.
	if err := h.call("net.configure", map[string]any{"sealed_port": []int{1}}, nil); code(err) != -32602 {
		t.Fatalf("a misspelt rule was accepted: %v", err)
	}
	h.must("net.configure", map[string]any{"services_ports": [][2]int{{sitePort, sitePort}}, "local_sites": "ask", "lan_allow": []string{}}, nil)
	err := h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": other.URL + "/"}, nil)
	if data := blocked(err); data == nil || data["decision"] != "ask" {
		t.Fatalf("an asked port: %v", err)
	}
	if reached != 0 {
		t.Fatalf("a refused port was reached %d times", reached)
	}
	h.must("net.grant", map[string]any{"group_id": "g1", "host": "127.0.0.1", "port": otherPort}, nil)
	var nav struct {
		Title string `json:"title"`
	}
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": other.URL + "/"}, &nav)
	if !strings.Contains(nav.Title, "the operator's own app") || reached == 0 {
		t.Fatalf("after the grant: %+v, reached %d", nav, reached)
	}
	h.must("net.revoke", map[string]any{"group_id": "g1", "host": "127.0.0.1", "port": otherPort}, nil)
	if data := blocked(h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": other.URL + "/?again"}, nil)); data == nil {
		t.Fatal("a revoked grant still opens")
	}
	if err := h.call("net.grant", map[string]any{"group_id": "nobody", "host": "127.0.0.1", "port": otherPort}, nil); code(err) != 1001 {
		t.Fatalf("a grant for no group: %v", err)
	}

	// With lan_sites "ask" a LAN address outside the list is a question for the host rather than a
	// refusal; the metadata service is refused as before.
	h.must("net.configure", map[string]any{"services_ports": [][2]int{{sitePort, sitePort}}, "local_sites": "ask", "lan_allow": []string{}, "lan_sites": "ask"}, nil)
	if data := blocked(h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://" + lanAddress(t) + "/"}, nil)); data == nil || data["decision"] != "ask" || data["reason"] != "private" {
		t.Fatalf("a LAN address with lan_sites ask: %v", data)
	}
	if data := blocked(h.call("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": "http://169.254.169.254/latest/meta-data/"}, nil)); data == nil || data["decision"] != "deny" || data["reason"] != "metadata" {
		t.Fatalf("the metadata service with lan_sites ask: %v", data)
	}
}

// lanAddress is a private address that is not one of this machine's own: a machine's own address is
// judged as this machine (a Docker bridge often holds 10.0.0.1), and that is a different rule.
func lanAddress(t *testing.T) string {
	own := map[string]bool{}
	addrs, _ := net.InterfaceAddrs()
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			own[n.IP.String()] = true
		}
	}
	for _, candidate := range []string{"10.254.254.254", "172.31.254.254", "10.123.45.67"} {
		if !own[candidate] {
			return candidate
		}
	}
	t.Skip("every candidate LAN address is this machine's own")
	return ""
}
