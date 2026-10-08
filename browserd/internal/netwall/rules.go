// Package netwall is the browser's one way out: an HTTP proxy inside the daemon that every request
// Chromium makes goes through, and the rules it judges them by.
//
// The rules are judged on the address a connection is actually made to, never on the name a page
// asked for. The proxy resolves the name itself, judges every address the answer holds, and dials
// only an address it judged: a name that answers with a public address when it is checked and a
// private one when it is used (DNS rebinding) cannot slip between the two, because there is only one
// lookup and the socket is opened on its result.
//
// Origin lists in browser automation (Playwright's allowed origins, Browser Use's allowed domains)
// have been walked around by redirects and by data: and blob: URLs, because they are checked in the
// code that navigates. This wall is below every navigation, script, worker and socket a page has.
// In a container it is the second wall: the container's network has no route to the key proxy or
// the agent in the first place. Natively it is the only one.
package netwall

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"strings"
)

// Decision is what the wall says about one destination.
type Decision string

const (
	Allow Decision = "allow"
	// Ask is a refusal the operator can lift: a grant for the destination (Proxy.Grant) opens it
	// for the browser. The agent sees it as a refusal that says it may ask.
	Ask  Decision = "ask"
	Deny Decision = "deny"
)

// The reasons a destination is refused. They travel in the 1102 error's data and in egress events,
// so the host can word its answer and the app can group them.
const (
	ReasonSealedPort   = "sealed_port"  // one of the installation's own doors: the API, the key proxy, a daemon, the launcher
	ReasonLoopback     = "loopback"     // another port on this machine
	ReasonGateway      = "gateway"      // the Docker host outside the services ranges
	ReasonPrivate      = "private"      // a private, carrier-grade NAT or unique-local address: the LAN
	ReasonLinkLocal    = "link_local"   // 169.254/16 and fe80::/10
	ReasonMetadata     = "metadata"     // a cloud provider's instance metadata service
	ReasonMulticast    = "multicast"    // multicast and broadcast
	ReasonReserved     = "reserved"     // unspecified, reserved, benchmarking, or an address that embeds one of the above
	ReasonUnresolvable = "unresolvable" // the name has no address
	ReasonScheme       = "scheme"       // file:, data:, blob:, javascript:, chrome: and the rest
	ReasonEgressAllow  = "egress_allow" // a top-level navigation outside the operator's allowlist
	ReasonLAN          = "lan_allow"    // a LAN address the operator listed: asked once, then open
	ReasonMixed        = "mixed"        // a name that answers with a public address and a private one at once
	ReasonBadTarget    = "bad_target"   // not a host and port the proxy can make sense of
)

// The values of Config.LocalSites.
const (
	LocalServices = "services"
	LocalAsk      = "ask"
	LocalAllow    = "allow"
)

// The values of Config.LANSites.
const (
	LANListed = "listed"
	LANAsk    = "ask"
)

// Limits on what the host may configure; they bound the work of every decision.
const (
	maxPorts       = 256
	maxRanges      = 32
	maxListEntries = 1024
	maxEntryLen    = 253
)

// Config is the wall's rules, as the host sends them in net.configure. The daemon starts with the
// zero Config, which is the strictest: public addresses only, nothing on this machine, nothing on the
// LAN. The host configures the wall before it opens a browser, and again when the operator's settings
// change.
type Config struct {
	// SealedPorts are refused on every address of this machine, whatever else says: the agent's
	// API, the key proxy, the daemons' hook listeners, the launcher's page. The proxy's own port is
	// always among them.
	SealedPorts []int `json:"sealed_ports"`
	// ServicesPorts are the ranges the agent's services and the terminals' servers listen in. They
	// are open on this machine's loopback natively, and on the Docker host through LoopbackRewrite in
	// a container.
	ServicesPorts [][2]int `json:"services_ports"`
	// LoopbackRewrite, in a container, is the name of the Docker host ("host.docker.internal"). A
	// loopback address in a services range is sent there instead, so a link the agent prints as
	// http://127.0.0.1:8103 opens the service it names; the host's address is open in the services
	// ranges and refused on every other port. Empty natively.
	LoopbackRewrite string `json:"loopback_rewrite,omitempty"`
	// LocalSites is what the other ports of this machine are — natively its loopback, in a container
	// the Docker host — outside the services ranges and the sealed ports: the operator's own local
	// apps and the dev servers the agent's coding members start. "services" (and empty) refuses
	// them, "ask" makes each an ask, "allow" opens them. A sealed port is refused whatever it says.
	LocalSites string `json:"local_sites"`
	// HostAddrs, in a container, are the Docker host's other addresses (its LAN address), judged as
	// the Docker host rather than as the private network: a link the agent prints as
	// http://<lan address>:5173 is the same local site as http://127.0.0.1:5173.
	HostAddrs []string `json:"host_addrs,omitempty"`
	// LANAllow lists LAN addresses (an address or a prefix: "172.20.1.20", "10.0.3.0/24") the
	// operator may open for the browser: each is asked, never open by itself. Metadata addresses
	// cannot be listed.
	LANAllow []string `json:"lan_allow"`
	// LANSites is what a private address outside LANAllow is: "listed" (and empty) refuses it,
	// "ask" makes it an ask like a listed one, so an agent that needs the router's page or a device
	// on the LAN asks for it rather than meeting a refusal nobody can lift. A link-local address
	// outside the list stays refused either way: on a LAN it is a device that failed to get an
	// address, and it is the neighbourhood the metadata services live in.
	LANSites string `json:"lan_sites,omitempty"`
	// EgressAllow, when present, is the operator's allowlist of hosts ("example.com",
	// "*.example.com"): a top-level navigation to any other host is asked. Subresources are not
	// judged by it (blocking them would break most pages) but every host is still in the egress log.
	// Absent means no allowlist.
	EgressAllow *[]string `json:"egress_allow,omitempty"`
}

// DecodeConfig reads net.configure's parameters strictly: an unknown field is an error, as for every
// other method of the daemon, so a host that misspells a rule learns it rather than running without.
func DecodeConfig(raw json.RawMessage) (Config, error) {
	var c Config
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return Config{}, err
	}
	if dec.More() {
		return Config{}, errors.New("trailing data after the parameters")
	}
	if _, err := c.compile(); err != nil {
		return Config{}, err
	}
	return c, nil
}

// rules is a Config made ready to judge with.
type rules struct {
	sealed    map[uint16]bool
	services  [][2]int
	rewrite   string
	local     string
	hostAddrs map[netip.Addr]bool
	lan       []netip.Prefix
	lanAsk    bool
	egress    []string
	hasEgress bool
}

func (c Config) compile() (*rules, error) {
	r := &rules{sealed: map[uint16]bool{}, rewrite: strings.ToLower(strings.TrimSuffix(strings.TrimSpace(c.LoopbackRewrite), ".")), hostAddrs: map[netip.Addr]bool{}}
	switch c.LocalSites {
	case "", LocalServices:
		r.local = LocalServices
	case LocalAsk, LocalAllow:
		r.local = c.LocalSites
	default:
		return nil, fmt.Errorf("local_sites: %q is not services, ask or allow", c.LocalSites)
	}
	switch c.LANSites {
	case "", LANListed:
	case LANAsk:
		r.lanAsk = true
	default:
		return nil, fmt.Errorf("lan_sites: %q is not listed or ask", c.LANSites)
	}
	if len(c.HostAddrs) > maxListEntries {
		return nil, fmt.Errorf("host_addrs: at most %d entries", maxListEntries)
	}
	for _, entry := range c.HostAddrs {
		a, err := netip.ParseAddr(strings.TrimSpace(entry))
		if err != nil {
			return nil, fmt.Errorf("host_addrs: %q is not an address", entry)
		}
		// A metadata or multicast address is judged by its class before the Docker host is asked
		// about, so listing one opens nothing.
		r.hostAddrs[a.Unmap()] = true
	}
	if len(c.SealedPorts) > maxPorts {
		return nil, fmt.Errorf("sealed_ports: at most %d", maxPorts)
	}
	for _, p := range c.SealedPorts {
		if p < 1 || p > 65535 {
			return nil, fmt.Errorf("sealed_ports: %d is not a port", p)
		}
		r.sealed[uint16(p)] = true
	}
	if len(c.ServicesPorts) > maxRanges {
		return nil, fmt.Errorf("services_ports: at most %d ranges", maxRanges)
	}
	for _, rg := range c.ServicesPorts {
		if rg[0] < 1 || rg[1] > 65535 || rg[0] > rg[1] {
			return nil, fmt.Errorf("services_ports: [%d, %d] is not a range of ports", rg[0], rg[1])
		}
		r.services = append(r.services, rg)
	}
	if len(r.rewrite) > maxEntryLen || strings.ContainsAny(r.rewrite, "/:@ ") {
		return nil, fmt.Errorf("loopback_rewrite: %q is not a host name", c.LoopbackRewrite)
	}
	if len(c.LANAllow) > maxListEntries {
		return nil, fmt.Errorf("lan_allow: at most %d entries", maxListEntries)
	}
	for _, entry := range c.LANAllow {
		prefix, err := parsePrefix(entry)
		if err != nil {
			return nil, fmt.Errorf("lan_allow: %w", err)
		}
		// A listed prefix that covers the metadata address would make it askable; it never is,
		// because the metadata rule is judged before the list is read.
		r.lan = append(r.lan, prefix)
	}
	if c.EgressAllow != nil {
		r.hasEgress = true
		if len(*c.EgressAllow) > maxListEntries {
			return nil, fmt.Errorf("egress_allow: at most %d entries", maxListEntries)
		}
		for _, entry := range *c.EgressAllow {
			entry = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(entry), "."))
			if entry == "" || len(entry) > maxEntryLen {
				return nil, fmt.Errorf("egress_allow: %q is not a host", entry)
			}
			r.egress = append(r.egress, entry)
		}
	}
	return r, nil
}

func parsePrefix(entry string) (netip.Prefix, error) {
	entry = strings.TrimSpace(entry)
	if strings.Contains(entry, "/") {
		p, err := netip.ParsePrefix(entry)
		if err != nil {
			return netip.Prefix{}, fmt.Errorf("%q is not an address or a prefix", entry)
		}
		return netip.PrefixFrom(p.Addr().Unmap(), p.Bits()).Masked(), nil
	}
	a, err := netip.ParseAddr(entry)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("%q is not an address or a prefix", entry)
	}
	a = a.Unmap()
	return netip.PrefixFrom(a, a.BitLen()), nil
}

func (r *rules) inServices(port int) bool {
	for _, rg := range r.services {
		if port >= rg[0] && port <= rg[1] {
			return true
		}
	}
	return false
}

// egressAllowed is the host's own matching (daedalus/host/policy.py, host_allowed): an exact host,
// or "*.example.com" for the domain and everything under it.
func (r *rules) egressAllowed(host string) bool {
	if !r.hasEgress {
		return true
	}
	for _, entry := range r.egress {
		if rest, ok := strings.CutPrefix(entry, "*."); ok {
			if host == rest || strings.HasSuffix(host, "."+rest) {
				return true
			}
		} else if host == entry {
			return true
		}
	}
	return false
}

func mustPrefixes(texts ...string) []netip.Prefix {
	out := make([]netip.Prefix, len(texts))
	for i, t := range texts {
		out[i] = netip.MustParsePrefix(t)
	}
	return out
}

// The address classes, judged in this order. An address that falls in none is public.
var (
	metadataAddrs = []netip.Addr{
		netip.MustParseAddr("169.254.169.254"), // AWS, GCP, Azure, OpenStack, DigitalOcean …
		netip.MustParseAddr("169.254.170.2"),   // the ECS task metadata endpoint
		netip.MustParseAddr("100.100.100.200"), // Alibaba Cloud
		netip.MustParseAddr("fd00:ec2::254"),   // AWS over IPv6
	}
	reservedPrefixes = mustPrefixes(
		"0.0.0.0/8",      // "this network": connecting to 0.0.0.0 reaches this machine on Linux
		"192.0.0.0/24",   // protocol assignments
		"198.18.0.0/15",  // benchmarking, used by some appliances for internal networks
		"240.0.0.0/4",    // reserved, and the limited broadcast address
		"::/128",         // unspecified
		"100::/64",       // discard
		"2001::/32",      // Teredo: an IPv4 address tunnelled inside
		"2001:db8::/32",  // documentation
		"64:ff9b:1::/48", // local-use NAT64
	)
	multicastPrefixes = mustPrefixes("224.0.0.0/4", "ff00::/8")
	linkLocalPrefixes = mustPrefixes("169.254.0.0/16", "fe80::/10")
	privatePrefixes   = append(mustPrefixes(
		"10.0.0.0/8",
		"172.16.0.0/12",
		"100.64.0.0/10", // carrier-grade NAT, and the address space of Tailscale-like overlays
		"fc00::/7",      // unique-local
		"fec0::/10",     // site-local, deprecated but still routed on some LANs
	),
		// The home LAN's range, written as bytes: the repository's public-text audit refuses the
		// dotted form anywhere, since in any other file it would be someone's address.
		netip.PrefixFrom(netip.AddrFrom4([4]byte{192, 168, 0, 0}), 16),
	)
	nat64Prefix      = netip.MustParsePrefix("64:ff9b::/96")
	sixToFour        = netip.MustParsePrefix("2002::/16")
	loopbackPrefixes = mustPrefixes("127.0.0.0/8", "::1/128")
)

// class is what an address is, before the rules for its port are applied.
type class int

const (
	classPublic class = iota
	classLoopback
	classPrivate
	classLinkLocal
	classMetadata
	classMulticast
	classReserved
)

// classify says what kind of address a is. An IPv4 address inside IPv6 (mapped, NAT64, 6to4) is
// judged as the IPv4 address it carries, so a private address cannot be spelled as a public-looking
// IPv6 one.
func classify(a netip.Addr) class {
	a = a.Unmap()
	if a.Is6() {
		if nat64Prefix.Contains(a) {
			b := a.As16()
			return classify(netip.AddrFrom4([4]byte{b[12], b[13], b[14], b[15]}))
		}
		if sixToFour.Contains(a) {
			b := a.As16()
			if inner := classify(netip.AddrFrom4([4]byte{b[2], b[3], b[4], b[5]})); inner != classPublic {
				return classReserved
			}
		}
	}
	for _, m := range metadataAddrs {
		if a == m {
			return classMetadata
		}
	}
	for _, p := range loopbackPrefixes {
		if p.Contains(a) {
			return classLoopback
		}
	}
	for _, p := range reservedPrefixes {
		if p.Contains(a) {
			return classReserved
		}
	}
	for _, p := range multicastPrefixes {
		if p.Contains(a) {
			return classMulticast
		}
	}
	for _, p := range linkLocalPrefixes {
		if p.Contains(a) {
			return classLinkLocal
		}
	}
	for _, p := range privatePrefixes {
		if p.Contains(a) {
			return classPrivate
		}
	}
	return classPublic
}

// Verdict is the wall's answer for one destination.
type Verdict struct {
	Decision Decision
	Reason   string // one of the Reason constants; empty when allowed
	Host     string // as asked, lower case
	Port     int
	// Addrs are the addresses the proxy may dial, judged; only when allowed.
	Addrs []netip.Addr
}

// Allowed reports whether the destination is open.
func (v Verdict) Allowed() bool { return v.Decision == Allow }

// Data is the 1102 error's data and the body of an egress event.
func (v Verdict) Data() map[string]any {
	return map[string]any{"host": v.Host, "port": v.Port, "decision": string(v.Decision), "reason": v.Reason}
}

// Message is the refusal as a sentence, for the error's message and the proxy's refusal page.
func (v Verdict) Message() string {
	where := fmt.Sprintf("%s:%d", v.Host, v.Port)
	switch v.Reason {
	case ReasonSealedPort:
		return where + " is one of this installation's own doors; the browser never reaches it"
	case ReasonLoopback:
		if v.Decision == Ask {
			return where + " is a port on this machine outside the services ranges; the operator can open it"
		}
		return where + " is a port on this machine outside the services ranges; the operator can open local sites in the browser settings"
	case ReasonGateway:
		if v.Decision == Ask {
			return where + " is a port on the Docker host outside the services ranges; the operator can open it"
		}
		return where + " is the Docker host outside the services ranges; the operator can open local sites in the browser settings"
	case ReasonPrivate:
		if v.Decision == Ask {
			return where + " is an address on the local network; it opens once the request for it is allowed"
		}
		return where + " is on a private network; the operator can let the browser ask for LAN addresses in the browser settings"
	case ReasonMixed:
		return v.Host + " answers with a public address and a private one at once, which is what a rebinding attack looks like"
	case ReasonLinkLocal:
		return where + " is a link-local address"
	case ReasonMetadata:
		return where + " is a cloud metadata service"
	case ReasonMulticast:
		return where + " is a multicast or broadcast address"
	case ReasonReserved:
		return where + " is a reserved address"
	case ReasonUnresolvable:
		return v.Host + " does not resolve"
	case ReasonScheme:
		return "the browser opens only http and https addresses"
	case ReasonEgressAllow:
		return v.Host + " is outside the allowed hosts; the operator can open it"
	case ReasonLAN:
		return where + " is a LAN address the operator listed; it opens once they allow it"
	case ReasonBadTarget:
		return where + " is not a destination the browser can open"
	}
	return where + " is open"
}

// severity orders decisions so that the strictest of several wins.
func severity(d Decision) int {
	switch d {
	case Deny:
		return 2
	case Ask:
		return 1
	}
	return 0
}
