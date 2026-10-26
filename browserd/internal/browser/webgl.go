package browser

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/chrome"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// WebGL of the agent's browser, chosen per profile (chrome.GraphicsFlags says what each mode starts
// Chromium with). The mode is kept in the profile beside its scripts and extensions, so it holds for
// every later start of that profile's browser until it is changed; a switch takes a restart, because
// how Chromium draws is fixed on its command line. The daemon only carries the choice out: whether
// the agent may make it is the host's business, which asks the operator first.
const (
	webglFile = "webgl.json"
	// ReasonRelaunch is the reason a browser closed only to start again in another mode: its groups
	// come back under the same ids, so the host keeps them open rather than closing their rows.
	ReasonRelaunch = "relaunch"
)

type webglChoice struct {
	Mode string `json:"mode"`
}

// webglPath is the profile's choice of mode.
func (m *Manager) webglPath(profile string) string {
	return filepath.Join(m.ProfileDir(profile), devtoolsDir, webglFile)
}

// ProfileWebGL is the mode the profile's browser starts in: off unless one was chosen. A file that
// does not parse is off, never a mode nobody chose.
func (m *Manager) ProfileWebGL(profile string) string {
	if profile == EphemeralProfile || m.Attached() {
		return chrome.WebGLOff
	}
	data, err := os.ReadFile(m.webglPath(profile))
	if err != nil {
		return chrome.WebGLOff
	}
	var c webglChoice
	if json.Unmarshal(data, &c) != nil || !chrome.ValidWebGL(c.Mode) {
		return chrome.WebGLOff
	}
	return c.Mode
}

// virtualDisplay says whether this daemon's browsers have no display of the machine's own: headless,
// or headed on the virtual display the daemon starts when it was given none.
func (m *Manager) virtualDisplay() bool {
	if !m.deps.Config.Chromium.Headed() {
		return true
	}
	return needsX() && os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == ""
}

// WebGLInfo is what daemon.info says of the modes: which a browser here can start in, and why gpu
// cannot when it cannot.
func (m *Manager) WebGLInfo() map[string]any {
	if m.Attached() {
		// The operator's Chrome draws as they set it up; nothing here changes how.
		return map[string]any{"modes": []string{}, "gpu": false, "gpu_reason": "the operator's own Chrome is not restarted by Daedalus"}
	}
	ok, why := chrome.GPUReady(runtime.GOOS, m.virtualDisplay())
	modes := []string{chrome.WebGLOff, chrome.WebGLSoftware}
	if ok {
		modes = append(modes, chrome.WebGLGPU)
	}
	return map[string]any{"modes": modes, "gpu": ok, "gpu_reason": why}
}

// WebGLResult is what a change of mode did.
type WebGLResult struct {
	Profile string `json:"profile"`
	Mode    string `json:"mode"`
	// Relaunched is true when the profile's browser was running and was started again in the mode.
	Relaunched bool   `json:"relaunched"`
	BrowserID  string `json:"browser_id,omitempty"`
	// Groups are the groups that came back, with their new tabs, and the pages that did not reopen.
	Groups []RelaunchedGroup `json:"groups"`
	// Graphics is the running browser's own word on WebGL (Graphics), nil when none runs.
	Graphics map[string]any `json:"graphics"`
}

// RelaunchedGroup is one group taken across a relaunch.
type RelaunchedGroup struct {
	ID     string           `json:"id"`
	Tabs   []map[string]any `json:"tabs"`
	Failed []string         `json:"failed,omitempty"`
}

// SetWebGL keeps mode as the profile's and, when its browser runs in another mode, starts it again in
// this one with its groups and their pages. An empty mode changes nothing and reports.
func (m *Manager) SetWebGL(ctx context.Context, profile, mode string) (WebGLResult, error) {
	if m.Attached() {
		return WebGLResult{}, ErrAttachRefused("changing how the browser draws")
	}
	if !ValidID(profile) {
		return WebGLResult{}, wire.Errorf(wire.CodeInvalidParams, "profile must be 1-64 of A-Z a-z 0-9 - _")
	}
	if profile == EphemeralProfile {
		return WebGLResult{}, wire.Errorf(wire.CodeUnsupported, "a throwaway browser starts as it always does; WebGL is chosen for a profile of your own")
	}
	if mode != "" && !chrome.ValidWebGL(mode) {
		return WebGLResult{}, wire.Errorf(wire.CodeInvalidParams, "mode is off, software or gpu")
	}
	if mode == chrome.WebGLGPU {
		if ok, why := chrome.GPUReady(runtime.GOOS, m.virtualDisplay()); !ok {
			return WebGLResult{}, errWith(wire.CodeUnsupported, map[string]any{"reason": "no_gpu"}, "gpu mode cannot reach a GPU here: %s", why)
		}
	}
	current := m.ProfileWebGL(profile)
	if mode == "" {
		mode = current
	}
	res := WebGLResult{Profile: profile, Mode: mode, Groups: []RelaunchedGroup{}}
	m.mu.Lock()
	b := m.byProfile[profile]
	m.mu.Unlock()
	if mode != current {
		if err := writeJSON(m.webglPath(profile), webglChoice{Mode: mode}); err != nil {
			return WebGLResult{}, err
		}
	}
	if b != nil && b.WebGL != mode {
		nb, groups, err := m.relaunch(ctx, b)
		if err != nil {
			return WebGLResult{}, err
		}
		b, res.Relaunched, res.Groups = nb, true, groups
	}
	if b != nil {
		res.BrowserID = b.ID
		res.Graphics = m.Graphics(ctx, b)
	}
	return res, nil
}

// relaunch closes b and starts its profile's browser again, opening each of its groups under the
// same id with the pages its tabs showed. A browser the operator holds is not taken from them.
func (m *Manager) relaunch(ctx context.Context, b *Browser) (*Browser, []RelaunchedGroup, error) {
	type kept struct {
		params OpenParams
		vp     Viewport
		urls   []string
		active int
	}
	var keep []kept
	for _, g := range b.groupList() {
		if g.Control().Owner == OwnerHuman {
			return nil, nil, wire.Errorf(wire.CodeForbidden, "the operator holds this browser; it restarts once they give it back")
		}
		k := kept{params: OpenParams{GroupID: g.ID, Profile: g.Profile, Labels: g.Labels}, vp: g.ViewportNow()}
		active := g.ActiveTab()
		for i, t := range g.Tabs() {
			if t == active {
				k.active = i
			}
			k.urls = append(k.urls, t.URL())
		}
		keep = append(keep, k)
	}
	b.setClosing(ReasonRelaunch)
	b.proc.Close(config.CloseGrace)
	<-b.gone
	nb, err := m.ensureBrowser(ctx, b.Profile)
	if err != nil {
		return nil, nil, err
	}
	out := []RelaunchedGroup{}
	for _, k := range keep {
		g, err := m.newGroup(ctx, nb, k.params, k.vp)
		if err != nil {
			return nb, out, err
		}
		rg := RelaunchedGroup{ID: g.ID}
		var tabs []*Tab
		for _, u := range k.urls {
			t, err := m.NewTab(ctx, g, "", false)
			if err != nil {
				rg.Failed = append(rg.Failed, u)
				continue
			}
			tabs = append(tabs, t)
			if u == "" || u == "about:blank" {
				continue
			}
			if _, err := m.Navigate(ctx, t, u, 30*time.Second); err != nil {
				// The page is the agent's to open again; the tab stays, blank, so the group keeps
				// its shape.
				rg.Failed = append(rg.Failed, u)
				m.log.Info("a page did not reopen after the relaunch", "group", g.ID, "url", u, "error", err.Error())
			}
		}
		if len(tabs) == 0 {
			t, err := m.NewTab(ctx, g, "", false)
			if err != nil {
				return nb, out, err
			}
			tabs = append(tabs, t)
		}
		if k.active < len(tabs) {
			_ = m.SelectTab(ctx, tabs[k.active], true)
		}
		for _, t := range tabs {
			rg.Tabs = append(rg.Tabs, t.View())
		}
		out = append(out, rg)
		m.publish("group.opened", map[string]any{"group_id": g.ID, "browser_id": nb.ID, "profile": g.Profile, "labels": g.Labels, "reason": ReasonRelaunch})
	}
	return nb, out, nil
}

// Graphics is the browser's own report on how it draws (chrome://gpu's, through SystemInfo): whether
// a page gets WebGL and what renders it. It is what tells a mode that was chosen from one
// that works: gpu mode on a machine whose GPU Chromium could not start falls back to no WebGL, and
// only this says so.
func (m *Manager) Graphics(ctx context.Context, b *Browser) map[string]any {
	if b.attached || b.conn == nil {
		return nil
	}
	cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	var info struct {
		GPU struct {
			FeatureStatus map[string]string `json:"featureStatus"`
			AuxAttributes map[string]any    `json:"auxAttributes"`
		} `json:"gpu"`
	}
	if err := b.conn.Call(cctx, "", "SystemInfo.getInfo", nil, &info); err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return map[string]any{"error": "the browser did not report its graphics in time"}
		}
		return map[string]any{"error": err.Error()}
	}
	status := info.GPU.FeatureStatus["webgl"]
	renderer, _ := info.GPU.AuxAttributes["glRenderer"].(string)
	return map[string]any{
		"mode": b.WebGL, "webgl": status, "available": webglAvailable(status), "renderer": renderer,
	}
}

// webglAvailable reads a feature status: "enabled", "enabled_readback" and their kin give a page a
// context, and so does "unavailable_software" — no hardware, SwiftShader drawing, which is what a
// headless browser reports in off mode while its pages do get WebGL (measured on 151); "disabled_off"
// and the other "_off" ones do not.
func webglAvailable(status string) bool {
	return strings.HasPrefix(status, "enabled") || strings.HasSuffix(status, "_software")
}
