package relay

import (
	"fmt"
	"strconv"
	"strings"
)

// The allowlist of DevTools methods a relay carries. The extension enforces its own copy, so a
// compromised daemon or bridge still cannot read the operator's cookies or reach a tab outside the
// agent's groups; the daemon checks the same list before it sends, so a call the extension would
// refuse fails at once with the method named, and the fake relay the tests run against enforces it
// too, which is what proves the daemon never needs more.
//
// What is refused is what reaches past the agent's own tabs or into the operator's credentials:
// the Browser domain and Target above a tab (both act on the whole browser), Storage (every site's
// data at once), the cookie methods of Network, and the download behaviour, which is browser-wide.

var allowedDomains = map[string]bool{
	"Page": true, "Runtime": true, "DOM": true, "DOMDebugger": true, "DOMSnapshot": true, "Input": true,
	"Accessibility": true, "Log": true, "Fetch": true, "Network": true,
}

var refusedMethods = map[string]bool{
	"Network.getCookies": true, "Network.getAllCookies": true, "Network.setCookie": true, "Network.setCookies": true,
	"Network.deleteCookies": true, "Network.clearBrowserCookies": true, "Network.clearBrowserCache": true,
	// Deprecated, and it sets the download behaviour of the whole browser context.
	"Page.setDownloadBehavior": true,
}

// Emulation is allowed for focus, media and the device metrics only: the agent's window keeps
// painting while unfocused, and nothing else of the operator's browser is pretended.
var allowedEmulation = map[string]bool{
	"Emulation.setFocusEmulationEnabled": true, "Emulation.setEmulatedMedia": true,
	"Emulation.setDeviceMetricsOverride": true, "Emulation.clearDeviceMetricsOverride": true,
}

// Target is allowed on the agent's own tabs only for following their frames of other sites.
var allowedTarget = map[string]bool{"Target.setAutoAttach": true, "Target.detachFromTarget": true}

// Allowed reports whether a relay carries method, and why not.
func Allowed(method string) error {
	domain, _, ok := strings.Cut(method, ".")
	if !ok || domain == "" {
		return fmt.Errorf("%q is not a DevTools method", method)
	}
	switch {
	case refusedMethods[method]:
	case allowedDomains[domain]:
		return nil
	case domain == "Emulation" && allowedEmulation[method]:
		return nil
	case domain == "Target" && allowedTarget[method]:
		return nil
	}
	return fmt.Errorf("%s is not carried to the operator's Chrome", method)
}

// SessionOf is the relay session of a tab, "x<tabId>".
func SessionOf(tab int) string { return "x" + strconv.Itoa(tab) }

// ParseSession reads a relay session: "x<tabId>" for a tab, "x<tabId>/<sessionId>" for a flat child
// session of one.
func ParseSession(s string) (tab int, child string, ok bool) {
	if !strings.HasPrefix(s, "x") {
		return 0, "", false
	}
	head, child, _ := strings.Cut(s[1:], "/")
	n, err := strconv.Atoi(head)
	if err != nil || n < 0 || strconv.Itoa(n) != head {
		return 0, "", false
	}
	if strings.Contains(s[1:], "/") && child == "" {
		return 0, "", false
	}
	return n, child, true
}
