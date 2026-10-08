package netwall

import (
	"context"
	"encoding/json"
	"errors"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeResolver answers from a table; a name it does not know does not resolve. The tests never ask
// a real resolver: they may not touch the network.
type fakeResolver struct {
	mu      sync.Mutex
	answers map[string][]string
	asked   []string
}

func (f *fakeResolver) LookupNetIP(_ context.Context, _, host string) ([]netip.Addr, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.asked = append(f.asked, host)
	texts, ok := f.answers[host]
	if !ok {
		return nil, errors.New("no such host")
	}
	out := make([]netip.Addr, len(texts))
	for i, t := range texts {
		out[i] = netip.MustParseAddr(t)
	}
	return out, nil
}

func (f *fakeResolver) set(host string, addrs ...string) {
	f.mu.Lock()
	f.answers[host] = addrs
	f.mu.Unlock()
}

func newTestWall(t *testing.T, c Config, answers map[string][]string) (*Wall, *fakeResolver, *[]Egress) {
	t.Helper()
	res := &fakeResolver{answers: answers}
	var mu sync.Mutex
	events := &[]Egress{}
	w := New(Options{
		Resolver: res,
		LocalAddrs: func() []netip.Addr {
			return []netip.Addr{netip.MustParseAddr("172.20.1.5"), netip.MustParseAddr("198.51.100.7")}
		},
		Events: func(e Egress) {
			mu.Lock()
			*events = append(*events, e)
			mu.Unlock()
		},
	})
	if err := w.Configure(c); err != nil {
		t.Fatal(err)
	}
	return w, res, events
}

func TestClassifyTable(t *testing.T) {
	cases := map[string]class{
		"8.8.8.8":                  classPublic,
		"203.0.113.10":             classPublic,
		"2606:4700::1111":          classPublic,
		"127.0.0.1":                classLoopback,
		"127.8.9.10":               classLoopback,
		"::1":                      classLoopback,
		"::ffff:127.0.0.1":         classLoopback,
		"10.0.0.1":                 classPrivate,
		"172.16.0.1":               classPrivate,
		"172.31.255.255":           classPrivate,
		"172.32.0.1":               classPublic,
		"100.64.0.1":               classPrivate,
		"fd12:3456::1":             classPrivate,
		"fec0::1":                  classPrivate,
		"169.254.1.1":              classLinkLocal,
		"fe80::1":                  classLinkLocal,
		"169.254.169.254":          classMetadata,
		"169.254.170.2":            classMetadata,
		"100.100.100.200":          classMetadata,
		"fd00:ec2::254":            classMetadata,
		"::ffff:169.254.169.254":   classMetadata,
		"0.0.0.0":                  classReserved,
		"0.1.2.3":                  classReserved,
		"::":                       classReserved,
		"255.255.255.255":          classReserved,
		"240.0.0.1":                classReserved,
		"198.18.0.1":               classReserved,
		"224.0.0.251":              classMulticast,
		"ff02::fb":                 classMulticast,
		"2001:0:4136:e378::1":      classReserved, // Teredo
		"64:ff9b::a00:1":           classPrivate,  // NAT64 of 10.0.0.1
		"64:ff9b::7f00:1":          classLoopback, // NAT64 of 127.0.0.1
		"64:ff9b::a9fe:a9fe":       classMetadata, // NAT64 of 169.254.169.254
		"64:ff9b::808:808":         classPublic,   // NAT64 of 8.8.8.8
		"2002:a00:1::1":            classReserved, // 6to4 of 10.0.0.1
		"2002:808:808::1":          classPublic,   // 6to4 of 8.8.8.8
		"2001:db8::1":              classReserved,
		"64:ff9b:1::1":             classReserved,
		"100::1":                   classReserved,
		"192.0.0.8":                classReserved,
		"224.0.0.1":                classMulticast,
		"239.255.255.250":          classMulticast,
		"::ffff:10.1.2.3":          classPrivate,
		"::ffff:172.20.10.10":      classPrivate,
		"::ffff:8.8.4.4":           classPublic,
		"2a00:1450:4001:80b::200e": classPublic,
	}
	for text, want := range cases {
		if got := classify(netip.MustParseAddr(text)); got != want {
			t.Errorf("classify(%s) = %v, want %v", text, got, want)
		}
	}
	// The home LAN's range, built from bytes for the reason rules.go gives.
	for _, a := range []netip.Addr{netip.AddrFrom4([4]byte{192, 168, 0, 1}), netip.AddrFrom4([4]byte{192, 168, 255, 254}), netip.AddrFrom16(netip.AddrFrom4([4]byte{192, 168, 1, 1}).As16())} {
		if got := classify(a); got != classPrivate {
			t.Errorf("classify(%s) = %v, want private", a, got)
		}
	}
}

func TestDecodeConfigIsStrict(t *testing.T) {
	good := `{"sealed_ports":[8765,3200],"services_ports":[[8100,8119],[8120,8139]],"loopback_rewrite":"host.docker.internal","local_sites":"services","host_addrs":["172.30.0.9"],"lan_allow":["172.20.1.20","10.0.3.0/24"],"lan_sites":"ask","egress_allow":["example.com","*.example.org"]}`
	c, err := DecodeConfig(json.RawMessage(good))
	if err != nil {
		t.Fatal(err)
	}
	if c.EgressAllow == nil || len(*c.EgressAllow) != 2 || len(c.ServicesPorts) != 2 {
		t.Fatalf("decoded %+v", c)
	}
	for _, bad := range []string{
		`{"sealed_port":[1]}`,                       // misspelt: refused, not ignored
		`{"sealed_ports":[0]}`,                      // not a port
		`{"sealed_ports":[70000]}`,                  // not a port
		`{"services_ports":[[8119,8100]]}`,          // backwards
		`{"lan_allow":["not-an-address"]}`,          // not an address
		`{"lan_allow":["10.0.0.0/33"]}`,             // not a prefix
		`{"loopback_rewrite":"http://gateway"}`,     // not a host name
		`{"egress_allow":[""]}`,                     // an empty host
		`{"sealed_ports":[1]} {"sealed_ports":[2]}`, // two objects
		`[]`,
	} {
		if _, err := DecodeConfig(json.RawMessage(bad)); err == nil {
			t.Errorf("DecodeConfig(%s) accepted", bad)
		}
	}
	// No egress_allow at all is no allowlist; an empty one allows nothing.
	none, _ := DecodeConfig(json.RawMessage(`{}`))
	empty, _ := DecodeConfig(json.RawMessage(`{"egress_allow":[]}`))
	if none.EgressAllow != nil || empty.EgressAllow == nil {
		t.Fatalf("absent and empty allowlists are different things: %v %v", none.EgressAllow, empty.EgressAllow)
	}
}

func TestDecodeGrant(t *testing.T) {
	g, err := DecodeGrant(json.RawMessage(`{"group_id":"g1","host":"LocalHost.","port":3000}`))
	if err != nil || g.TTL() != time.Hour {
		t.Fatalf("%+v %v", g, err)
	}
	for _, bad := range []string{`{"group_id":"g1","host":"x","port":0}`, `{"group_id":"","host":"x","port":1}`, `{"group_id":"g","host":"x","port":1,"ttl_ms":90000000}`, `{"group_id":"g","host":"x","port":1,"until":1}`} {
		if _, err := DecodeGrant(json.RawMessage(bad)); err == nil {
			t.Errorf("DecodeGrant(%s) accepted", bad)
		}
	}
}

// nativeConfig is what the host sends natively: its own doors sealed, the services ranges open on
// this machine, the operator's other local ports asked.
var nativeConfig = Config{SealedPorts: []int{8765, 3200, 47001}, ServicesPorts: [][2]int{{8100, 8119}, {8120, 8139}}, LocalSites: LocalAsk, LANAllow: []string{"172.20.1.20", "10.9.0.0/16"}}

// containerConfig is what it sends to the container's daemon: loopback in the services ranges goes
// to the Docker host.
var containerConfig = Config{SealedPorts: []int{8765}, ServicesPorts: [][2]int{{8100, 8119}, {8120, 8139}}, LoopbackRewrite: "host.docker.internal"}

func TestNativeRules(t *testing.T) {
	w, _, _ := newTestWall(t, nativeConfig, map[string][]string{
		"example.com":         {"203.0.113.10"},
		"rebind.example":      {"10.0.0.1"},
		"mixed.example":       {"203.0.113.11", "172.20.0.10"},
		"metadata.example":    {"169.254.169.254"},
		"nas.example":         {"172.20.1.20"},
		"six.example":         {"::ffff:10.0.0.1"},
		"nat64.example":       {"64:ff9b::a9fe:a9fe"},
		"zero.example":        {"0.0.0.0"},
		"self-public.example": {"198.51.100.7"},
	})
	p, err := w.Listen("b1")
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()
	ctx := context.Background()
	type want struct {
		d      Decision
		reason string
	}
	cases := []struct {
		host string
		port int
		want want
	}{
		{"example.com", 443, want{Allow, ""}},
		{"EXAMPLE.com.", 443, want{Allow, ""}},
		{"203.0.113.10", 80, want{Allow, ""}},
		// DNS rebinding: a public-looking name that answers with a LAN address is judged by the
		// address.
		{"rebind.example", 80, want{Deny, ReasonPrivate}},
		{"mixed.example", 80, want{Deny, ReasonPrivate}},
		{"six.example", 80, want{Deny, ReasonPrivate}},
		{"10.0.0.1", 80, want{Deny, ReasonPrivate}},
		{"172.17.0.1", 80, want{Deny, ReasonPrivate}},
		{"[fd00::1]", 80, want{Deny, ReasonPrivate}},
		{"169.254.169.254", 80, want{Deny, ReasonMetadata}},
		{"metadata.example", 80, want{Deny, ReasonMetadata}},
		{"nat64.example", 80, want{Deny, ReasonMetadata}},
		{"169.254.1.1", 80, want{Deny, ReasonLinkLocal}},
		{"fe80::1%eth0", 80, want{Deny, ReasonLinkLocal}},
		{"zero.example", 8765, want{Deny, ReasonReserved}},
		{"0.0.0.0", 80, want{Deny, ReasonReserved}},
		{"224.0.0.251", 5353, want{Deny, ReasonMulticast}},
		// The installation's own doors, on every address of this machine.
		{"127.0.0.1", 8765, want{Deny, ReasonSealedPort}},
		{"localhost", 3200, want{Deny, ReasonSealedPort}},
		{"api.localhost", 47001, want{Deny, ReasonSealedPort}},
		{"[::1]", 8765, want{Deny, ReasonSealedPort}},
		{"127.1.2.3", 8765, want{Deny, ReasonSealedPort}},
		{"172.20.1.5", 8765, want{Deny, ReasonSealedPort}},
		{"198.51.100.7", 8765, want{Deny, ReasonSealedPort}},
		{"self-public.example", 3200, want{Deny, ReasonSealedPort}},
		{"127.0.0.1", p.Port(), want{Deny, ReasonSealedPort}},
		// The services ranges are open on this machine; its other ports are asked.
		{"127.0.0.1", 8103, want{Allow, ""}},
		{"localhost", 8139, want{Allow, ""}},
		{"172.20.1.5", 8110, want{Allow, ""}},
		{"127.0.0.1", 3000, want{Ask, ReasonLoopback}},
		{"198.51.100.7", 22, want{Ask, ReasonLoopback}},
		// A listed LAN address is asked, an unlisted one refused.
		{"172.20.1.20", 80, want{Ask, ReasonLAN}},
		{"nas.example", 5000, want{Ask, ReasonLAN}},
		{"10.9.8.7", 443, want{Ask, ReasonLAN}},
		{"172.20.1.21", 80, want{Deny, ReasonPrivate}},
		{"nowhere.example", 80, want{Deny, ReasonUnresolvable}},
		{"", 80, want{Deny, ReasonBadTarget}},
		{"example.com", 0, want{Deny, ReasonBadTarget}},
	}
	for _, c := range cases {
		v := w.judge(ctx, p, c.host, c.port)
		if v.Decision != c.want.d || v.Reason != c.want.reason {
			t.Errorf("%s:%d = %s/%s, want %s/%s", c.host, c.port, v.Decision, v.Reason, c.want.d, c.want.reason)
		}
		if v.Allowed() != (len(v.Addrs) > 0) {
			t.Errorf("%s:%d: allowed %v with addresses %v", c.host, c.port, v.Allowed(), v.Addrs)
		}
	}
}

func TestGrantsOpenAnAskForOneBrowserUntilTheyEnd(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	res := &fakeResolver{answers: map[string][]string{"nas.example": {"172.20.1.20"}}}
	w := New(Options{Resolver: res, LocalAddrs: func() []netip.Addr { return nil }, Now: func() time.Time { return now }})
	if err := w.Configure(nativeConfig); err != nil {
		t.Fatal(err)
	}
	a, _ := w.Listen("a")
	b, _ := w.Listen("b")
	defer a.Close()
	defer b.Close()
	ctx := context.Background()
	a.Grant("LOCALHOST", 3000, now.Add(time.Minute))
	a.Grant("nas.example", 5000, now.Add(48*time.Hour)) // capped at a day
	if !w.judge(ctx, a, "localhost", 3000).Allowed() || !w.judge(ctx, a, "nas.example", 5000).Allowed() {
		t.Fatal("a grant did not open the asked destination")
	}
	if w.judge(ctx, b, "localhost", 3000).Allowed() {
		t.Fatal("a grant for one browser opened it for another")
	}
	if w.judge(ctx, a, "localhost", 3001).Allowed() || w.judge(ctx, a, "127.0.0.1", 3000).Allowed() {
		t.Fatal("a grant opened more than the host and port it names")
	}
	// A grant never lifts a refusal: sealed ports and unlisted LAN addresses stay shut.
	a.Grant("127.0.0.1", 8765, now.Add(time.Hour))
	a.Grant("10.0.0.1", 80, now.Add(time.Hour))
	if w.judge(ctx, a, "127.0.0.1", 8765).Allowed() || w.judge(ctx, a, "10.0.0.1", 80).Allowed() {
		t.Fatal("a grant opened a refusal")
	}
	now = now.Add(2 * time.Minute)
	if w.judge(ctx, a, "localhost", 3000).Allowed() {
		t.Fatal("an expired grant still opens")
	}
	if !w.judge(ctx, a, "nas.example", 5000).Allowed() {
		t.Fatal("a day's grant ended after two minutes")
	}
	now = now.Add(25 * time.Hour)
	if w.judge(ctx, a, "nas.example", 5000).Allowed() {
		t.Fatal("a grant outlived its one-day cap")
	}
	a.Grant("localhost", 3000, now.Add(time.Hour))
	a.Revoke("localhost", 3000)
	if w.judge(ctx, a, "localhost", 3000).Allowed() {
		t.Fatal("a revoked grant still opens")
	}
}

func TestContainerRules(t *testing.T) {
	w, res, _ := newTestWall(t, containerConfig, map[string][]string{
		"host.docker.internal": {"172.18.0.1"},
		"keyproxy":             {"172.19.0.3"}, // were it resolvable at all: it is on another network
		"example.com":          {"203.0.113.10"},
	})
	p, _ := w.Listen("b1")
	defer p.Close()
	ctx := context.Background()
	for _, c := range []struct {
		host   string
		port   int
		d      Decision
		reason string
	}{
		{"127.0.0.1", 8103, Allow, ""},
		{"localhost", 8125, Allow, ""},
		{"host.docker.internal", 8110, Allow, ""},
		{"172.18.0.1", 8119, Allow, ""},
		{"host.docker.internal", 8765, Deny, ReasonSealedPort},
		{"172.18.0.1", 22, Deny, ReasonGateway},
		{"127.0.0.1", 8765, Deny, ReasonSealedPort},
		{"127.0.0.1", 3000, Deny, ReasonLoopback},
		{"keyproxy", 3200, Deny, ReasonPrivate},
		{"searxng", 8080, Deny, ReasonUnresolvable},
		{"169.254.169.254", 80, Deny, ReasonMetadata},
		{"example.com", 443, Allow, ""},
	} {
		v := w.judge(ctx, p, c.host, c.port)
		if v.Decision != c.d || v.Reason != c.reason {
			t.Errorf("%s:%d = %s/%s, want %s/%s", c.host, c.port, v.Decision, v.Reason, c.d, c.reason)
		}
	}
	// The rewrite dials the Docker host's address, never the container's own loopback.
	v := w.judge(ctx, p, "127.0.0.1", 8103)
	if len(v.Addrs) != 1 || v.Addrs[0] != netip.MustParseAddr("172.18.0.1") {
		t.Fatalf("the services rewrite dials %v", v.Addrs)
	}
	for _, asked := range res.asked {
		if asked == "127.0.0.1" || asked == "localhost" {
			t.Fatalf("a loopback name was asked of the resolver: %v", res.asked)
		}
	}
}

// The operator's local sites: the Docker host's other ports in a container, this machine's natively.
// The sealed ports and the metadata address stay shut whatever the setting says.
func TestLocalSites(t *testing.T) {
	names := map[string][]string{"host.docker.internal": {"172.18.0.1"}, "dev.example": {"172.30.0.9"}}
	judge := func(c Config, host string, port int, grant bool) (Decision, string, []netip.Addr) {
		w, _, _ := newTestWall(t, c, names)
		p, _ := w.Listen("b1")
		defer p.Close()
		if grant {
			p.Grant(host, port, time.Now().Add(time.Hour))
		}
		v := w.judge(context.Background(), p, host, port)
		return v.Decision, v.Reason, v.Addrs
	}
	container := containerConfig
	container.HostAddrs = []string{"172.30.0.9"}
	for _, c := range []struct {
		local  string
		host   string
		port   int
		grant  bool
		d      Decision
		reason string
	}{
		{LocalServices, "127.0.0.1", 5173, false, Deny, ReasonLoopback},
		{LocalServices, "172.30.0.9", 5173, false, Deny, ReasonGateway},
		{LocalAsk, "127.0.0.1", 5173, false, Ask, ReasonGateway},
		{LocalAsk, "localhost", 5173, true, Allow, ""},
		{LocalAsk, "172.30.0.9", 8840, false, Ask, ReasonGateway},
		{LocalAsk, "dev.example", 8840, true, Allow, ""},
		{LocalAllow, "127.0.0.1", 5173, false, Allow, ""},
		{LocalAllow, "172.30.0.9", 3000, false, Allow, ""},
		{LocalAllow, "127.0.0.1", 8765, false, Deny, ReasonSealedPort},
		{LocalAllow, "172.30.0.9", 8765, false, Deny, ReasonSealedPort},
		{LocalAllow, "169.254.169.254", 80, false, Deny, ReasonMetadata},
		{LocalAllow, "172.30.0.10", 80, false, Deny, ReasonPrivate},
	} {
		cfg := container
		cfg.LocalSites = c.local
		d, reason, addrs := judge(cfg, c.host, c.port, c.grant)
		if d != c.d || reason != c.reason {
			t.Errorf("container %s: %s:%d = %s/%s, want %s/%s", c.local, c.host, c.port, d, reason, c.d, c.reason)
		}
		if d == Allow && c.host == "127.0.0.1" && (len(addrs) != 1 || addrs[0] != netip.MustParseAddr("172.18.0.1")) {
			t.Errorf("container %s: %s:%d dials %v, not the Docker host", c.local, c.host, c.port, addrs)
		}
	}
	native := nativeConfig
	for local, want := range map[string]Decision{LocalServices: Deny, LocalAsk: Ask, LocalAllow: Allow} {
		native.LocalSites = local
		if d, _, _ := judge(native, "127.0.0.1", 5173, false); d != want {
			t.Errorf("native %s: 127.0.0.1:5173 = %s, want %s", local, d, want)
		}
		if d, reason, _ := judge(native, "127.0.0.1", 8765, false); d != Deny || reason != ReasonSealedPort {
			t.Errorf("native %s: the sealed port is %s/%s", local, d, reason)
		}
	}
	if _, err := DecodeConfig(json.RawMessage(`{"local_sites":"always"}`)); err == nil {
		t.Error("an unknown local_sites was accepted")
	}
	if _, err := DecodeConfig(json.RawMessage(`{"host_addrs":["not-an-address"]}`)); err == nil {
		t.Error("a host address that is not one was accepted")
	}
}

// The LAN beyond the operator's list: refused while lan_sites is "listed" (and before the host says
// anything), asked when it is "ask", and open after a grant for exactly the host and port asked. The
// metadata services, link-local addresses, a name that also answers public and the installation's own
// doors stay refused whatever it says, granted or not.
func TestLANSites(t *testing.T) {
	names := map[string][]string{"router.example": {"10.20.0.1"}, "mixed.example": {"203.0.113.11", "10.20.0.1"}, "metadata.example": {"169.254.169.254"}}
	judge := func(c Config, host string, port int, grant bool) (Decision, string) {
		w, _, _ := newTestWall(t, c, names)
		p, _ := w.Listen("b1")
		defer p.Close()
		if grant {
			p.Grant(host, port, time.Now().Add(time.Hour))
		}
		v := w.judge(context.Background(), p, host, port)
		return v.Decision, v.Reason
	}
	asking := nativeConfig
	asking.LANSites = LANAsk
	for _, c := range []struct {
		cfg    Config
		host   string
		port   int
		grant  bool
		d      Decision
		reason string
	}{
		{Config{}, "10.20.0.1", 80, false, Deny, ReasonPrivate},
		{nativeConfig, "10.20.0.1", 80, false, Deny, ReasonPrivate},
		{nativeConfig, "10.20.0.1", 80, true, Deny, ReasonPrivate},
		{asking, "10.20.0.1", 80, false, Ask, ReasonPrivate},
		{asking, "router.example", 443, false, Ask, ReasonPrivate},
		{asking, "[fd00::1]", 8080, false, Ask, ReasonPrivate},
		{asking, "10.20.0.1", 80, true, Allow, ""},
		{asking, "router.example", 443, true, Allow, ""},
		// A grant is for the host and port asked, not for the address or another port.
		{asking, "10.20.0.1", 8080, false, Ask, ReasonPrivate},
		{asking, "172.20.1.20", 80, false, Ask, ReasonLAN},
		{asking, "mixed.example", 80, false, Deny, ReasonMixed},
		{asking, "mixed.example", 80, true, Deny, ReasonMixed},
		{asking, "169.254.169.254", 80, true, Deny, ReasonMetadata},
		{asking, "metadata.example", 80, true, Deny, ReasonMetadata},
		{asking, "169.254.1.1", 80, true, Deny, ReasonLinkLocal},
		{asking, "172.20.1.5", 8765, true, Deny, ReasonSealedPort},
	} {
		if d, reason := judge(c.cfg, c.host, c.port, c.grant); d != c.d || reason != c.reason {
			t.Errorf("lan_sites %q, %s:%d (granted %v) = %s/%s, want %s/%s", c.cfg.LANSites, c.host, c.port, c.grant, d, reason, c.d, c.reason)
		}
	}
	if _, err := DecodeConfig(json.RawMessage(`{"lan_sites":"all"}`)); err == nil {
		t.Error("an unknown lan_sites was accepted")
	}
}

func TestNavigationSchemesAndTheAllowlist(t *testing.T) {
	allow := []string{"example.com", "*.docs.example"}
	c := nativeConfig
	c.EgressAllow = &allow
	w, _, events := newTestWall(t, c, map[string][]string{
		"example.com":     {"203.0.113.10"},
		"www.example.com": {"203.0.113.10"},
		"docs.example":    {"203.0.113.12"},
		"a.docs.example":  {"203.0.113.12"},
		"other.example":   {"203.0.113.13"},
	})
	p, _ := w.Listen("b1")
	defer p.Close()
	ctx := context.Background()
	for _, c := range []struct {
		url    string
		d      Decision
		reason string
	}{
		{"about:blank", Allow, ""},
		{"https://example.com/a?b", Allow, ""},
		{"HTTP://Example.COM:80/", Allow, ""},
		{"https://docs.example/", Allow, ""},
		{"https://a.docs.example/", Allow, ""},
		{"https://www.example.com/", Ask, ReasonEgressAllow},
		{"https://other.example/", Ask, ReasonEgressAllow},
		{"http://127.0.0.1:8103/", Allow, ""}, // the installation's services are not the web
		{"http://127.0.0.1:8765/api", Deny, ReasonSealedPort},
		{"http://169.254.169.254/latest/meta-data/", Deny, ReasonMetadata},
		{"file:///etc/passwd", Deny, ReasonScheme},
		{"data:text/html,<script>fetch('//x')</script>", Deny, ReasonScheme},
		{"blob:https://example.com/0f6d0a39-3b3c-4b7c-9c1a-1c6a1f0b3f11", Deny, ReasonScheme},
		{"javascript:alert(1)", Deny, ReasonScheme},
		{"chrome://settings", Deny, ReasonScheme},
		{"chrome-extension://abc/x.html", Deny, ReasonScheme},
		{"view-source:https://example.com/", Deny, ReasonScheme},
		{"devtools://devtools/bundled/inspector.html", Deny, ReasonScheme},
		{"filesystem:https://example.com/temporary/x", Deny, ReasonScheme},
		{"ws://example.com/socket", Deny, ReasonScheme},
		{"https:///nohost", Deny, ReasonScheme},
		{"http://example.com:99999/", Deny, ReasonBadTarget},
		{"http://example.com:abc/", Deny, ReasonScheme},
	} {
		v := w.CheckNavigation(ctx, p, c.url)
		if v.Decision != c.d || v.Reason != c.reason {
			t.Errorf("%s = %s/%s, want %s/%s", c.url, v.Decision, v.Reason, c.d, c.reason)
		}
	}
	// The operator's answer to an allowlist ask opens that host for the browser.
	p.Grant("other.example", 443, time.Now().Add(time.Hour))
	if !w.CheckNavigation(ctx, p, "https://other.example/x").Allowed() {
		t.Fatal("a grant did not answer the allowlist's ask")
	}
	// Every refusal and the first visit to each host reached the egress log, once a minute each.
	seen := map[string]int{}
	for _, e := range *events {
		seen[e.Host+"/"+string(e.Decision)]++
		if e.Browser != "b1" {
			t.Fatalf("an egress event without its browser: %+v", e)
		}
	}
	// example.com was opened on two ports, 80 and 443: one event each.
	if seen["example.com/allow"] != 2 || seen["other.example/ask"] != 1 || seen["file:/deny"] != 1 {
		t.Fatalf("egress events: %v", seen)
	}
}

func TestEgressEventsAreRateLimitedPerHostAndDecision(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	var got []Egress
	w := New(Options{Resolver: &fakeResolver{answers: map[string][]string{"example.com": {"203.0.113.10"}}}, LocalAddrs: func() []netip.Addr { return nil }, Now: func() time.Time { return now }, Events: func(e Egress) { got = append(got, e) }})
	p, _ := w.Listen("b1")
	defer p.Close()
	for i := 0; i < 50; i++ {
		w.CheckNavigation(context.Background(), p, "https://example.com/"+strings.Repeat("a", i))
	}
	if len(got) != 1 {
		t.Fatalf("%d events for one host in a minute", len(got))
	}
	now = now.Add(61 * time.Second)
	w.CheckNavigation(context.Background(), p, "https://example.com/")
	if len(got) != 2 || got[1].At != now.UnixMilli() {
		t.Fatalf("events after a minute: %+v", got)
	}
}

func TestTheStrictestRulesUntilConfigured(t *testing.T) {
	w := New(Options{Resolver: &fakeResolver{answers: map[string][]string{"example.com": {"203.0.113.10"}}}, LocalAddrs: func() []netip.Addr { return nil }})
	p, _ := w.Listen("b1")
	defer p.Close()
	ctx := context.Background()
	if !w.judge(ctx, p, "example.com", 443).Allowed() {
		t.Fatal("public addresses are open before any configuration")
	}
	for _, target := range []struct {
		host string
		port int
	}{{"127.0.0.1", 8103}, {"localhost", 3000}, {"172.20.1.1", 80}, {"10.0.0.1", 80}} {
		if v := w.judge(ctx, p, target.host, target.port); v.Decision != Deny {
			t.Errorf("%s:%d is %s before any configuration", target.host, target.port, v.Decision)
		}
	}
}
