package browser

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// Page scripts are the agent's own code and styles, run in every document of its browser whose
// address matches, before the page's first script: what a userscript manager does, for testing a
// change to a site (its look, a function replaced) before building it into an extension or the site
// itself. They belong to the browser's profile, like its extensions and logins, so every tab of it
// gets them, the ones opened later included, and they are kept in the profile until removed: a
// browser closed for idling has them again when it starts.
const (
	MaxScripts     = 32
	MaxScriptBytes = 256 << 10
	// scriptsFile is where a profile keeps them, beside the extensions, in a folder of the daemon's
	// own inside the profile: Chromium leaves a folder it does not know alone, and clearing or
	// deleting the profile takes them with everything else of it.
	devtoolsDir = "browserd"
	scriptsFile = "scripts.json"
)

var scriptName = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// PageScript is one of them as it is kept.
type PageScript struct {
	Name string `json:"name"`
	// Matches are the addresses it runs on: globs over the whole address (https://example.com/*,
	// *://localhost:*/*), or a bare host, which means that host and its subdomains on any scheme and
	// port. None runs it everywhere.
	Matches []string `json:"matches,omitempty"`
	JS      string   `json:"js,omitempty"`
	CSS     string   `json:"css,omitempty"`
	// AllFrames runs it in every frame of a page, not only the top document.
	AllFrames bool `json:"all_frames,omitempty"`
	// Source is where the host said it came from (a file of the agent's, or inline), shown back.
	Source  string    `json:"source,omitempty"`
	AddedAt time.Time `json:"added_at"`

	patterns []*regexp.Regexp
}

// View is the script as a listing shows it: what it is and where it runs, not its text.
func (s PageScript) View() map[string]any {
	v := map[string]any{"name": s.Name, "matches": s.Matches, "js_bytes": len(s.JS), "css_bytes": len(s.CSS),
		"added_at": s.AddedAt}
	if s.Matches == nil {
		v["matches"] = []string{}
	}
	if s.AllFrames {
		v["all_frames"] = true
	}
	if s.Source != "" {
		v["source"] = s.Source
	}
	return v
}

// matchSource turns one pattern into a regular expression both Go and JavaScript read the same way
// (regexp.QuoteMeta escapes only what both treat as an escape).
func matchSource(p string) (string, error) {
	p = strings.TrimSpace(p)
	switch {
	case p == "" || p == "<all_urls>" || p == "*":
		return "", nil
	case len(p) > 500:
		return "", fmt.Errorf("a match pattern is at most 500 characters")
	case !strings.Contains(p, "/") && !strings.Contains(p, "*"):
		// A bare host: the site and its subdomains, as an operator says "on example.com".
		host := strings.ToLower(strings.TrimPrefix(p, "."))
		return `^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#@]*\.)?` + regexp.QuoteMeta(host) + `(:[0-9]+)?([/?#]|$)`, nil
	}
	return "^" + strings.ReplaceAll(regexp.QuoteMeta(p), `\*`, ".*") + "$", nil
}

// compile checks the script and prepares its patterns.
func (s *PageScript) compile() error {
	if !scriptName.MatchString(s.Name) {
		return wire.Errorf(wire.CodeInvalidParams, "a script's name is 1-64 of A-Z a-z 0-9 . _ -")
	}
	if s.JS == "" && s.CSS == "" {
		return wire.Errorf(wire.CodeInvalidParams, "a script needs js, css or both")
	}
	if len(s.JS)+len(s.CSS) > MaxScriptBytes {
		return wire.Errorf(wire.CodeInvalidParams, "a script's js and css are at most %d bytes together", MaxScriptBytes)
	}
	if len(s.Matches) > 32 {
		return wire.Errorf(wire.CodeInvalidParams, "at most 32 match patterns")
	}
	s.patterns = nil
	for _, p := range s.Matches {
		src, err := matchSource(p)
		if err != nil {
			return wire.Errorf(wire.CodeInvalidParams, "%v", err)
		}
		if src == "" {
			// Everywhere, whatever else is listed.
			s.patterns = nil
			return nil
		}
		re, err := regexp.Compile(src)
		if err != nil {
			return wire.Errorf(wire.CodeInvalidParams, "match pattern %q: %v", p, err)
		}
		s.patterns = append(s.patterns, re)
	}
	return nil
}

// Runs reports whether the script runs on a document at url.
func (s PageScript) Runs(url string) bool {
	if len(s.patterns) == 0 {
		return true
	}
	for _, re := range s.patterns {
		if re.MatchString(url) {
			return true
		}
	}
	return false
}

// source is what Chromium is given: the agent's code behind the address check, its styles first.
// The code runs inside an async function so a top-level await works and an exception it throws is
// told in the console under the script's name rather than as an anonymous error; the styles are a
// constructed sheet, which a page's Content-Security-Policy does not refuse the way it refuses an
// inline <style>, and which needs no <head> to exist yet at document start.
func (s PageScript) source() string {
	var pats []string
	for _, re := range s.patterns {
		pats = append(pats, re.String())
	}
	if pats == nil {
		pats = []string{}
	}
	patsJSON, _ := json.Marshal(pats)
	nameJSON, _ := json.Marshal(s.Name)
	var b strings.Builder
	b.WriteString("(async () => {\n")
	fmt.Fprintf(&b, "const patterns = %s.map((p) => new RegExp(p));\n", patsJSON)
	b.WriteString("if (patterns.length && !patterns.some((p) => p.test(location.href))) return;\n")
	if !s.AllFrames {
		b.WriteString("if (window !== window.top) return;\n")
	}
	if s.CSS != "" {
		cssJSON, _ := json.Marshal(s.CSS)
		fmt.Fprintf(&b, "try { const sheet = new CSSStyleSheet(); sheet.replaceSync(%s); "+
			"document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet]; } "+
			"catch (e) { console.error(\"page script \" + %s + \": its css was not applied:\", e); }\n", cssJSON, nameJSON)
	}
	if s.JS != "" {
		b.WriteString("try {\n")
		b.WriteString(s.JS)
		fmt.Fprintf(&b, "\n} catch (e) { console.error(\"page script \" + %s + \" threw:\", e); }\n", nameJSON)
	}
	b.WriteString("})();\n//# sourceURL=page-script/" + s.Name + ".js\n")
	return b.String()
}

// devtoolsPath is a file of the daemon's own in the browser's profile.
func (b *Browser) devtoolsPath(name string) string {
	return filepath.Join(b.profileDir, devtoolsDir, name)
}

// loadScripts reads the profile's scripts when its browser starts. A file that does not parse is
// set aside rather than trusted or lost.
func (b *Browser) loadScripts() {
	if b.profileDir == "" {
		return
	}
	data, err := os.ReadFile(b.devtoolsPath(scriptsFile))
	if err != nil {
		return
	}
	var list []PageScript
	if json.Unmarshal(data, &list) != nil {
		_ = os.Rename(b.devtoolsPath(scriptsFile), b.devtoolsPath(scriptsFile+".unreadable"))
		return
	}
	kept := list[:0]
	for _, s := range list {
		if s.compile() == nil {
			kept = append(kept, s)
		}
	}
	b.mu.Lock()
	b.scripts = kept
	b.mu.Unlock()
}

func (b *Browser) saveScripts(list []PageScript) error {
	if b.profileDir == "" {
		return nil
	}
	return writeJSON(b.devtoolsPath(scriptsFile), list)
}

func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(v, "", " ")
	if err != nil {
		return err
	}
	tmp := path + ".new"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Scripts are the browser's page scripts, by name.
func (b *Browser) Scripts() []PageScript {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := append([]PageScript(nil), b.scripts...)
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func (b *Browser) hasFrameScripts() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, s := range b.scripts {
		if s.AllFrames {
			return true
		}
	}
	return false
}

// ScriptsOn names the scripts that run on a document at url: what a result says is active on a page.
func (b *Browser) ScriptsOn(url string) []string {
	var out []string
	for _, s := range b.Scripts() {
		if s.Runs(url) {
			out = append(out, s.Name)
		}
	}
	return out
}

// scriptSessions is where a tab keeps the identifiers Chromium gave its scripts, by session (the
// page's and each frame's of another site) and name, so a script removed or replaced is taken back
// from every document it was given to.
type scriptSessions map[string]map[string]string

func (t *Tab) setScriptID(session, name, id string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.scriptIDs == nil {
		t.scriptIDs = scriptSessions{}
	}
	if t.scriptIDs[session] == nil {
		t.scriptIDs[session] = map[string]string{}
	}
	t.scriptIDs[session][name] = id
}

func (t *Tab) takeScriptIDs(name string) map[string]string {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := map[string]string{}
	for session, ids := range t.scriptIDs {
		if id, ok := ids[name]; ok {
			out[session] = id
			delete(ids, name)
		}
	}
	return out
}

func (t *Tab) dropScriptSession(session string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.scriptIDs, session)
}

// pendingScript is a script sent to a session whose identifier is still on its way.
type pendingScript struct {
	name  string
	reply <-chan cdpReply
}

// sendScripts sends a session the browser's scripts without waiting, for a page being set up (whose
// renderer answers only once it is let run). frame is a frame of another site, which gets only the
// scripts that run in every frame.
func (m *Manager) sendScripts(ctx context.Context, b *Browser, session string, frame bool) []pendingScript {
	var out []pendingScript
	for _, s := range b.Scripts() {
		if frame && !s.AllFrames {
			continue
		}
		out = append(out, pendingScript{name: s.Name, reply: startCall(ctx, b, session, "Page.addScriptToEvaluateOnNewDocument",
			map[string]any{"source": s.source()})})
	}
	return out
}

// recordScripts keeps the identifiers sendScripts' replies carry.
func (m *Manager) recordScripts(t *Tab, session string, pending []pendingScript) {
	for _, p := range pending {
		r := <-p.reply
		if r.err != nil {
			m.log.Warn("page script", "tab", t.ID, "script", p.name, "error", r.err.Error())
			continue
		}
		var id struct {
			Identifier string `json:"identifier"`
		}
		if json.Unmarshal(r.result, &id) == nil && id.Identifier != "" {
			t.setScriptID(session, p.name, id.Identifier)
		}
	}
}

// browserTabs is every tab of a browser, across its groups.
func (b *Browser) browserTabs() []*Tab {
	var out []*Tab
	for _, g := range b.groupList() {
		out = append(out, g.Tabs()...)
	}
	return out
}

// errScriptsRefused is the operator's Chrome: the agent works read-only there, and code of its own
// in the operator's signed-in pages is exactly what that rules out.
func errScriptsRefused(what string) *wire.Error {
	return ErrAttachRefused(what)
}

// AddScript keeps a page script in the group's browser, replacing one of the same name, and gives
// it to every open tab for its next document. check, when set, is the tab whose page compiles the
// code first, so a syntax error is told now rather than as a silent nothing on the next load.
func (m *Manager) AddScript(ctx context.Context, g *Group, s PageScript, check *Tab) ([]PageScript, error) {
	b := g.Browser
	if b.attached {
		return nil, errScriptsRefused("page scripts")
	}
	if err := s.compile(); err != nil {
		return nil, err
	}
	if s.AddedAt.IsZero() {
		s.AddedAt = time.Now().UTC()
	}
	if check != nil && s.JS != "" {
		var r struct {
			ExceptionDetails *struct {
				Text       string `json:"text"`
				LineNumber int    `json:"lineNumber"`
				Exception  *struct {
					Description string `json:"description"`
				} `json:"exception"`
			} `json:"exceptionDetails"`
		}
		err := check.Call(ctx, "Runtime.compileScript", map[string]any{"expression": s.source(), "sourceURL": "page-script/" + s.Name + ".js",
			"persistScript": false}, &r)
		if err == nil && r.ExceptionDetails != nil {
			msg := r.ExceptionDetails.Text
			if r.ExceptionDetails.Exception != nil && r.ExceptionDetails.Exception.Description != "" {
				msg = r.ExceptionDetails.Exception.Description
			}
			// Told in the agent's own lines, not the wrapper's.
			line := r.ExceptionDetails.LineNumber + 1 - scriptPrologue(s)
			return nil, wire.Errorf(wire.CodeInvalidParams, "the script does not parse (line %d of its js): %s", max(line, 1), firstLine(msg))
		}
	}
	b.mu.Lock()
	list := append([]PageScript(nil), b.scripts...)
	replaced := false
	for i := range list {
		if list[i].Name == s.Name {
			list[i], replaced = s, true
		}
	}
	if !replaced {
		if len(list) >= MaxScripts {
			b.mu.Unlock()
			return nil, errLimit("scripts", MaxScripts, "this browser has %d page scripts, the most it keeps; remove one", len(list))
		}
		list = append(list, s)
	}
	if err := b.saveScripts(list); err != nil {
		b.mu.Unlock()
		return nil, fmt.Errorf("keeping the script in the profile: %w", err)
	}
	b.scripts = list
	b.mu.Unlock()
	for _, t := range b.browserTabs() {
		m.retractScript(ctx, b, t, s.Name)
		sessions := []string{t.Session}
		if s.AllFrames {
			sessions = append(sessions, t.frameSessions()...)
		}
		for _, session := range sessions {
			var id struct {
				Identifier string `json:"identifier"`
			}
			if err := b.conn.Call(ctx, session, "Page.addScriptToEvaluateOnNewDocument", map[string]any{"source": s.source()}, &id); err != nil {
				m.log.Warn("page script", "tab", t.ID, "script", s.Name, "error", err.Error())
				continue
			}
			t.setScriptID(session, s.Name, id.Identifier)
		}
	}
	return b.Scripts(), nil
}

// RemoveScript takes a page script (every one, with name empty) out of the browser and its tabs.
// The documents already showing keep what it did until they load again.
func (m *Manager) RemoveScript(ctx context.Context, g *Group, name string) ([]string, error) {
	b := g.Browser
	if b.attached {
		return nil, errScriptsRefused("page scripts")
	}
	b.mu.Lock()
	var kept []PageScript
	var removed []string
	for _, s := range b.scripts {
		if name == "" || s.Name == name {
			removed = append(removed, s.Name)
			continue
		}
		kept = append(kept, s)
	}
	if name != "" && len(removed) == 0 {
		b.mu.Unlock()
		return nil, wire.Errorf(wire.CodeNotFound, "no page script %q", name)
	}
	if err := b.saveScripts(kept); err != nil {
		b.mu.Unlock()
		return nil, fmt.Errorf("updating the profile's scripts: %w", err)
	}
	b.scripts = kept
	b.mu.Unlock()
	for _, t := range b.browserTabs() {
		for _, n := range removed {
			m.retractScript(ctx, b, t, n)
		}
	}
	return removed, nil
}

func (m *Manager) retractScript(ctx context.Context, b *Browser, t *Tab, name string) {
	for session, id := range t.takeScriptIDs(name) {
		_ = b.conn.Call(ctx, session, "Page.removeScriptToEvaluateOnNewDocument", map[string]any{"identifier": id}, nil)
	}
}

func scriptPrologue(s PageScript) int {
	n := 4 // the function, the patterns, the address check, the try
	if !s.AllFrames {
		n++
	}
	if s.CSS != "" {
		n++
	}
	return n
}

func firstLine(s string) string {
	s, _, _ = strings.Cut(strings.TrimSpace(s), "\n")
	return s
}

// cdpReply is a command's result as startCall hands it over.
type cdpReply struct {
	result json.RawMessage
	err    error
}

// startCall writes one command now and hands its result over when it comes.
func startCall(ctx context.Context, b *Browser, session, method string, params any) <-chan cdpReply {
	out := make(chan cdpReply, 1)
	in := b.conn.Start(ctx, session, method, params)
	go func() {
		r := <-in
		out <- cdpReply{result: r.Result, err: r.Err}
	}()
	return out
}
