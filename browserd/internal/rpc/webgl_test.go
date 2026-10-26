//go:build unix

package rpc_test

import (
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
)

// A profile's WebGL mode is kept for its next start, and changing it while its browser runs starts
// the browser again in the new mode with its groups under the same ids and its pages reopened. The
// browser's own report says whether a page gets WebGL: software mode is SwiftShader, which every
// Chromium ships, so this holds on any machine.
func TestWebGLModeRelaunchesTheBrowserWithItsTabs(t *testing.T) {
	launchOnly(t, "the operator's Chrome draws as they set it up and is never restarted")
	h := start(t, nil)
	var info struct {
		Capabilities struct {
			WebGL struct {
				Modes     []string `json:"modes"`
				GPU       bool     `json:"gpu"`
				GPUReason string   `json:"gpu_reason"`
			} `json:"webgl"`
		} `json:"capabilities"`
	}
	h.must("daemon.info", nil, &info)
	modes := info.Capabilities.WebGL
	if !slices.Contains(modes.Modes, "off") || !slices.Contains(modes.Modes, "software") || slices.Contains(modes.Modes, "gpu") != modes.GPU {
		t.Fatalf("modes: %+v", modes)
	}
	if !modes.GPU && modes.GPUReason == "" {
		t.Fatalf("gpu is not offered and nothing says why: %+v", modes)
	}

	// Chosen before the browser runs: kept, nothing to restart.
	var r browser.WebGLResult
	h.must("profile.webgl", map[string]any{"profile": "p1", "mode": "software"}, &r)
	if r.Mode != "software" || r.Relaunched || r.Graphics != nil {
		t.Fatalf("before a start: %+v", r)
	}
	o := h.open("g1", "p1", "/still")
	var tab map[string]any
	h.must("tab.new", map[string]any{"group_id": "g1", "url": h.site.URL + "/button"}, &tab)
	h.must("profile.webgl", map[string]any{"profile": "p1"}, &r)
	renderer, _ := r.Graphics["renderer"].(string)
	if r.Mode != "software" || r.Relaunched || r.Graphics["available"] != true || !strings.Contains(renderer, "SwiftShader") {
		t.Fatalf("software mode's report: %+v", r)
	}
	webgl := h.evaluate(tab["id"].(string), "(() => { const c = document.createElement('canvas'); return String(!!c.getContext('webgl2') && !!document.createElement('canvas').getContext('webgl')) })()")
	if webgl.Value != "true" {
		t.Fatalf("a page in software mode has no WebGL: %+v", webgl)
	}
	var browsers struct {
		Browsers []map[string]any `json:"browsers"`
	}
	h.must("browser.list", nil, &browsers)
	if len(browsers.Browsers) != 1 || browsers.Browsers[0]["webgl"] != "software" {
		t.Fatalf("browsers: %+v", browsers)
	}
	before := browsers.Browsers[0]["id"]

	// Changed while it runs: a new browser, the same group, the same pages, the same active tab.
	h.must("profile.webgl", map[string]any{"profile": "p1", "mode": "off"}, &r)
	if !r.Relaunched || r.Mode != "off" || r.BrowserID == "" || r.BrowserID == before || len(r.Groups) != 1 {
		t.Fatalf("relaunch: %+v", r)
	}
	g := r.Groups[0]
	if g.ID != "g1" || len(g.Tabs) != 2 || len(g.Failed) != 0 {
		t.Fatalf("the group after the relaunch: %+v", g)
	}
	if !strings.HasSuffix(g.Tabs[0]["url"].(string), "/still") || !strings.HasSuffix(g.Tabs[1]["url"].(string), "/button") || g.Tabs[1]["active"] != true {
		t.Fatalf("the pages after the relaunch: %+v", g.Tabs)
	}
	if g.Tabs[0]["id"] == o.Tab.ID {
		t.Fatalf("a tab of the old browser survived: %+v", g.Tabs)
	}
	var groups struct {
		Groups []map[string]any `json:"groups"`
	}
	h.must("group.list", nil, &groups)
	if len(groups.Groups) != 1 || groups.Groups[0]["id"] != "g1" || groups.Groups[0]["browser_id"] != r.BrowserID {
		t.Fatalf("groups: %+v", groups)
	}
	var profiles struct {
		Profiles []browser.ProfileInfo `json:"profiles"`
	}
	h.must("profile.list", nil, &profiles)
	if len(profiles.Profiles) != 1 || profiles.Profiles[0].WebGL != "off" || !profiles.Profiles[0].Running {
		t.Fatalf("profiles: %+v", profiles)
	}
	// The same mode again restarts nothing.
	h.must("profile.webgl", map[string]any{"profile": "p1", "mode": "off"}, &r)
	if r.Relaunched {
		t.Fatalf("an unchanged mode restarted the browser: %+v", r)
	}

	// What is refused: a mode nobody knows, a throwaway browser, and gpu where no GPU is reachable.
	if err := h.call("profile.webgl", map[string]any{"profile": "p1", "mode": "on"}, nil); code(err) != -32602 {
		t.Fatalf("an unknown mode: %v", err)
	}
	if err := h.call("profile.webgl", map[string]any{"profile": browser.EphemeralProfile, "mode": "software"}, nil); code(err) != 1007 {
		t.Fatalf("a throwaway browser: %v", err)
	}
	if !modes.GPU {
		if err := h.call("profile.webgl", map[string]any{"profile": "p1", "mode": "gpu"}, nil); code(err) != 1007 || !strings.Contains(err.Error(), "GPU") {
			t.Fatalf("gpu with no GPU: %v", err)
		}
	}
}

// gpu mode on a machine with a GPU passed in: BROWSERD_TEST_GPU=1 runs it, in the browser image
// built with BROWSER_GPU=1 and given the render node and its group, which is how the service runs it.
// The renderer must be the GPU's, not one of the two CPU ones a failed start falls back to.
func TestWebGLGPUModeDrawsOnTheGPU(t *testing.T) {
	launchOnly(t, "the operator's Chrome is never restarted")
	if os.Getenv("BROWSERD_TEST_GPU") != "1" {
		t.Skip("BROWSERD_TEST_GPU=1 runs it where a GPU is passed in")
	}
	h := start(t, nil)
	var r browser.WebGLResult
	h.must("profile.webgl", map[string]any{"profile": "p1", "mode": "gpu"}, &r)
	o := h.open("g1", "p1", "/still")
	h.must("profile.webgl", map[string]any{"profile": "p1"}, &r)
	renderer, _ := r.Graphics["renderer"].(string)
	if r.Graphics["available"] != true || renderer == "" || strings.Contains(renderer, "SwiftShader") || strings.Contains(renderer, "llvmpipe") {
		t.Fatalf("gpu mode's report: %+v", r)
	}
	webgl := h.evaluate(o.Tab.ID, "String(!!document.createElement('canvas').getContext('webgl2'))")
	if webgl.Value != "true" {
		t.Fatalf("a page in gpu mode has no WebGL 2: %+v", webgl)
	}
	t.Logf("gpu mode renders on %s (%s)", renderer, r.Graphics["webgl"])
}
