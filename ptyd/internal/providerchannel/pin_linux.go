//go:build linux

package providerchannel

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"strings"
	"time"
)

type resolver interface {
	LookupIPAddr(context.Context, string) ([]net.IPAddr, error)
}

// PinnedProvider names the one HTTPS authority and the one IP selected before a launch. The
// socket broker dials the numeric IP directly, so a later DNS answer cannot redirect the tunnel.
type PinnedProvider struct {
	authority string
	address   netip.Addr
}

// PinProvider refuses mixed public and local DNS answers rather than selecting a convenient one.
// A new launch must resolve again; an existing launch never re-resolves an address while active.
func PinProvider(ctx context.Context, authority string, lookup resolver) (PinnedProvider, error) {
	host, port, err := net.SplitHostPort(authority)
	if err != nil || port != "443" || !providerHost(host) {
		return PinnedProvider{}, errors.New("provider channel: expected a DNS authority on port 443")
	}
	if lookup == nil {
		lookup = net.DefaultResolver
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	answers, err := lookup.LookupIPAddr(ctx, host)
	if err != nil || len(answers) == 0 {
		return PinnedProvider{}, errors.New("provider channel: DNS did not return an address")
	}
	var selected netip.Addr
	for _, answer := range answers {
		address, ok := netip.AddrFromSlice(answer.IP)
		if !ok || answer.Zone != "" || !publicProviderAddress(address) {
			return PinnedProvider{}, errors.New("provider channel: DNS returned a nonpublic address")
		}
		if !selected.IsValid() {
			selected = address.Unmap()
		}
	}
	return PinnedProvider{authority: host + ":443", address: selected}, nil
}

func providerHost(host string) bool {
	if host == "" || len(host) > 253 || host != strings.ToLower(host) || !strings.Contains(host, ".") ||
		strings.HasSuffix(host, ".") {
		return false
	}
	if _, err := netip.ParseAddr(host); err == nil {
		return false
	}
	for _, label := range strings.Split(host, ".") {
		if label == "" || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, char := range label {
			if (char < 'a' || char > 'z') && (char < '0' || char > '9') && char != '-' {
				return false
			}
		}
	}
	return true
}

var excludedProviderNetworks = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"), netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"), netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("192.88.99.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("224.0.0.0/4"),
	netip.MustParsePrefix("240.0.0.0/4"), netip.MustParsePrefix("64:ff9b::/96"),
	netip.MustParsePrefix("64:ff9b:1::/48"), netip.MustParsePrefix("2001::/32"),
	netip.MustParsePrefix("2001:db8::/32"), netip.MustParsePrefix("2002::/16"),
}

func publicProviderAddress(address netip.Addr) bool {
	if !address.IsValid() {
		return false
	}
	address = address.Unmap()
	if address.Is6() && !netip.MustParsePrefix("2000::/3").Contains(address) {
		return false
	}
	if !address.IsGlobalUnicast() || address.IsPrivate() || address.IsLoopback() ||
		address.IsLinkLocalUnicast() || address.IsMulticast() || address.IsUnspecified() {
		return false
	}
	for _, excluded := range excludedProviderNetworks {
		if excluded.Contains(address) {
			return false
		}
	}
	return true
}
