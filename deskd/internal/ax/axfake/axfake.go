// Package axfake is a stand-in for agent-desktop: a command line that answers the commands the
// daemon runs with the tool's JSON envelope, over windows and trees read from a scene file. It runs
// as a real child process, as the tool does, so the daemon's tests exercise the process handling
// (the environment, the deadline and the kill, a crash, an answer that is not JSON) along with the
// rules. The Go tests run it by re-executing their own binary; `deskd axfake` runs it for the
// fake desktop. It is never shipped as the tool and drives nothing.
package axfake

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/deskd/internal/ax"
)

// SceneEnv names the scene file in the child's environment.
const SceneEnv = "DESKD_AXFAKE_SCENE"

// Scene is the desktop as the fake tool sees it.
type Scene struct {
	// Version and Envelope are what `version` answers (1.0.2 and 2.5 by default).
	Version  string   `json:"version,omitempty"`
	Envelope string   `json:"envelope,omitempty"`
	Windows  []Window `json:"windows"`
	// Crash makes every command but version exit 3 with no JSON; Garbage prints text that is not
	// JSON and exits 0; HangMs sleeps that long before answering.
	Crash   bool `json:"crash,omitempty"`
	Garbage bool `json:"garbage,omitempty"`
	HangMs  int  `json:"hang_ms,omitempty"`
	// Log is a file every run appends one line to: its arguments, the AGENT_DESKTOP_ variables it
	// saw, and for an action what it did.
	Log string `json:"log,omitempty"`
}

// Window is one window of the tool's.
type Window struct {
	ID     string       `json:"id"`
	Title  string       `json:"title,omitempty"`
	App    string       `json:"app_name,omitempty"`
	PID    int          `json:"pid"`
	Bounds *ax.ToolRect `json:"bounds,omitempty"`
	Tree   Node         `json:"tree"`
	// SurfaceWindow is the window a snapshot of a surface of this window answers from.
	SurfaceWindow string `json:"surface_window,omitempty"`
}

// Node is an element of a fake tree: the tool's fields, and how an action on it answers.
type Node struct {
	Role             string       `json:"role"`
	Name             *string      `json:"name,omitempty"`
	Value            *string      `json:"value,omitempty"`
	States           []string     `json:"states,omitempty"`
	AvailableActions []string     `json:"available_actions,omitempty"`
	Bounds           *ax.ToolRect `json:"bounds,omitempty"`
	Children         []Node       `json:"children,omitempty"`
	// Error is the tool's error code an action on the element answers; Delivery the disposition
	// that answer carries (delivered_verified on success, not_delivered on an error by default).
	Error    string `json:"error,omitempty"`
	Delivery string `json:"delivery,omitempty"`
}

// LogLine is one run, as the scene's log records it.
type LogLine struct {
	Args   []string `json:"args"`
	Env    []string `json:"env,omitempty"`
	Home   string   `json:"home,omitempty"`
	Action string   `json:"action,omitempty"`
	Ref    string   `json:"ref,omitempty"`
	Text   string   `json:"text,omitempty"`
	Window string   `json:"window,omitempty"`
}

// saved is a snapshot as the fake keeps it between runs, in the state root as the tool does.
type saved struct {
	Window string           `json:"window"`
	Refs   map[string][]int `json:"refs"`
	Next   int              `json:"next"`
}

type run struct {
	scene Scene
	home  string
	out   io.Writer
	line  LogLine
}

// Main runs one command and returns the exit code: 0, 1 for a structured error, 2 for an argument
// error, as the tool does.
func Main(args []string, out io.Writer) int {
	r := &run{out: out, home: os.Getenv("AGENT_DESKTOP_HOME")}
	r.line.Args = args
	r.line.Home = r.home
	for _, kv := range os.Environ() {
		if name, _, _ := strings.Cut(kv, "="); strings.HasPrefix(name, "AGENT_DESKTOP_") {
			r.line.Env = append(r.line.Env, name)
		}
	}
	if b, err := os.ReadFile(os.Getenv(SceneEnv)); err == nil {
		if err := json.Unmarshal(b, &r.scene); err != nil {
			fmt.Fprintln(out, "bad scene")
			return 3
		}
	}
	defer r.log()
	if len(args) == 0 {
		return r.fail("INVALID_ARGS", "no command", "not_delivered", 2)
	}
	cmd, rest := args[0], args[1:]
	if cmd != "version" {
		if r.scene.Crash {
			fmt.Fprintln(out, "thread 'main' panicked")
			return 3
		}
		if r.scene.Garbage {
			fmt.Fprintln(out, "this is not json")
			return 0
		}
		if r.scene.HangMs > 0 {
			time.Sleep(time.Duration(r.scene.HangMs) * time.Millisecond)
		}
	}
	f, pos, err := parse(rest)
	if err != nil {
		return r.fail("INVALID_ARGS", err.Error(), "not_delivered", 2)
	}
	switch cmd {
	case "version":
		v := r.scene.Version
		if v == "" {
			v = "1.0.2"
		}
		return r.ok(cmd, map[string]any{"version": v, "target": "x86_64", "os": "linux"})
	case "list-windows":
		ws := []map[string]any{}
		for _, w := range r.scene.Windows {
			m := map[string]any{"id": w.ID, "title": w.Title, "app_name": w.App, "pid": w.PID, "is_focused": false}
			if w.Bounds != nil {
				m["bounds"] = w.Bounds
			}
			ws = append(ws, m)
		}
		return r.ok(cmd, ws)
	case "snapshot":
		return r.snapshot(f)
	case "find":
		return r.find(f)
	case "get", "is":
		return r.get(cmd, f, pos)
	case "click", "set-value", "clear", "type", "select", "toggle", "check", "uncheck", "expand", "collapse",
		"scroll", "scroll-to", "focus":
		return r.act(cmd, pos)
	}
	return r.fail("INVALID_ARGS", "the fake does not know "+cmd, "not_delivered", 2)
}

// parse reads flags the way the tool's parser does for the commands the daemon runs: a flag takes
// the next argument or the part after "=", "--" ends the flags, and a global flag the daemon must
// never pass is an error the tests look for.
func parse(args []string) (map[string]string, []string, error) {
	f := map[string]string{}
	var pos []string
	valued := map[string]bool{"--timeout-ms": true, "--window-id": true, "--root": true, "--surface": true, "--property": true,
		"--direction": true, "--amount": true, "--limit": true, "--role": true, "--name": true, "--value": true, "--text": true,
		"--max-depth": true}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			pos = append(pos, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(a, "-") {
			pos = append(pos, a)
			continue
		}
		name, val, hasVal := strings.Cut(a, "=")
		switch {
		case name == "--headed" || name == "--force":
			return nil, nil, fmt.Errorf("%s reached the tool", name)
		case valued[name] && hasVal:
			f[name] = val
		case valued[name]:
			if i+1 >= len(args) {
				return nil, nil, fmt.Errorf("%s needs a value", name)
			}
			i++
			f[name] = args[i]
		default:
			f[name] = "true"
		}
	}
	return f, pos, nil
}

func (r *run) log() {
	if r.scene.Log == "" {
		return
	}
	b, _ := json.Marshal(r.line)
	fh, err := os.OpenFile(r.scene.Log, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return
	}
	defer fh.Close()
	_, _ = fh.Write(append(b, '\n'))
}

func (r *run) envelope(v map[string]any) {
	v["version"] = "2.5"
	if r.scene.Envelope != "" {
		v["version"] = r.scene.Envelope
	}
	b, _ := json.Marshal(v)
	fmt.Fprintln(r.out, string(b))
}

func (r *run) ok(cmd string, data any) int {
	r.envelope(map[string]any{"ok": true, "command": cmd, "data": data})
	return 0
}

func (r *run) fail(code, msg, delivery string, exit int) int {
	return r.failWith(code, msg, delivery, nil, exit)
}

func (r *run) failWith(code, msg, delivery string, details any, exit int) int {
	retry := "unsafe"
	if delivery == "not_delivered" {
		retry = "safe"
	}
	e := map[string]any{"code": code, "message": msg, "disposition": map[string]any{"delivery": delivery, "retry": retry}}
	if details != nil {
		e["details"] = details
	}
	r.envelope(map[string]any{"ok": false, "error": e})
	return exit
}

func (r *run) window(id string) (*Window, bool) {
	for i := range r.scene.Windows {
		if r.scene.Windows[i].ID == id {
			return &r.scene.Windows[i], true
		}
	}
	return nil, false
}

func newSnapshotID() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return "s" + hex.EncodeToString(b)
}

func (r *run) savePath(id string) string { return filepath.Join(r.home, "snapshots", id+".json") }

func (r *run) load(id string) (*saved, bool) {
	b, err := os.ReadFile(r.savePath(id))
	if err != nil {
		return nil, false
	}
	var s saved
	if json.Unmarshal(b, &s) != nil {
		return nil, false
	}
	return &s, true
}

func (r *run) save(id string, s *saved) error {
	if err := os.MkdirAll(filepath.Join(r.home, "snapshots"), 0o700); err != nil {
		return err
	}
	b, _ := json.Marshal(s)
	return os.WriteFile(r.savePath(id), b, 0o600)
}

func nodeAt(root *Node, path []int) (*Node, bool) {
	n := root
	for _, i := range path {
		if i < 0 || i >= len(n.Children) {
			return nil, false
		}
		n = &n.Children[i]
	}
	return n, true
}

// emit writes a node in the tool's shape, giving every element with an action a reference.
func emit(n *Node, path []int, snap string, s *saved, depth, maxDepth int, bounds bool) map[string]any {
	out := map[string]any{"role": n.Role}
	if n.Name != nil {
		out["name"] = *n.Name
	}
	if n.Value != nil && !ax.IsSecure(n.Role, n.States) {
		out["value"] = *n.Value
	}
	if len(n.States) > 0 {
		out["states"] = n.States
	}
	if len(n.AvailableActions) > 0 {
		out["available_actions"] = n.AvailableActions
		s.Next++
		ref := "@" + snap + ":e" + strconv.Itoa(s.Next)
		s.Refs[ref] = append([]int(nil), path...)
		out["ref_id"] = ref
	}
	if bounds && n.Bounds != nil {
		out["bounds"] = n.Bounds
	}
	if len(n.Children) > 0 {
		if maxDepth > 0 && depth >= maxDepth {
			out["children_count"] = len(n.Children)
			return out
		}
		var kids []map[string]any
		for i := range n.Children {
			kids = append(kids, emit(&n.Children[i], append(append([]int(nil), path...), i), snap, s, depth+1, maxDepth, bounds))
		}
		out["children"] = kids
	}
	return out
}

func (r *run) snapshot(f map[string]string) int {
	maxDepth := 0
	if f["--skeleton"] == "true" {
		maxDepth = 3
	}
	bounds := f["--include-bounds"] == "true"
	if root := f["--root"]; root != "" {
		snap, path, s, w, code := r.resolve(root)
		if code != "" {
			return r.fail(code, "cannot resolve "+root, "not_delivered", 1)
		}
		n, _ := nodeAt(&w.Tree, path)
		tree := emit(n, path, snap, s, 0, maxDepth, bounds)
		if err := r.save(snap, s); err != nil {
			return r.fail("INTERNAL", err.Error(), "not_delivered", 1)
		}
		return r.ok("snapshot", map[string]any{"app": w.App, "window": map[string]any{"id": w.ID, "title": w.Title},
			"ref_count": len(s.Refs), "snapshot_id": snap, "complete": true, "tree": tree})
	}
	w, ok := r.window(f["--window-id"])
	if !ok {
		return r.fail("WINDOW_NOT_FOUND", "no window "+f["--window-id"], "not_delivered", 1)
	}
	answerFrom := w.ID
	if sf := f["--surface"]; sf != "" && sf != "window" && w.SurfaceWindow != "" {
		answerFrom = w.SurfaceWindow
	}
	snap := newSnapshotID()
	s := &saved{Window: w.ID, Refs: map[string][]int{}}
	tree := emit(&w.Tree, nil, snap, s, 0, maxDepth, bounds)
	if err := r.save(snap, s); err != nil {
		return r.fail("INTERNAL", err.Error(), "not_delivered", 1)
	}
	return r.ok("snapshot", map[string]any{"app": w.App, "window": map[string]any{"id": answerFrom, "title": w.Title},
		"ref_count": len(s.Refs), "snapshot_id": snap, "complete": true, "tree": tree})
}

// resolve finds a reference's element: its snapshot, its path, the saved snapshot and the window.
func (r *run) resolve(ref string) (string, []int, *saved, *Window, string) {
	snap, ok := ax.SnapshotOf(ref)
	if !ok {
		return "", nil, nil, nil, "INVALID_ARGS"
	}
	s, ok := r.load(snap)
	if !ok {
		return "", nil, nil, nil, "SNAPSHOT_NOT_FOUND"
	}
	path, ok := s.Refs[ref]
	if !ok {
		return "", nil, nil, nil, "STALE_REF"
	}
	w, ok := r.window(s.Window)
	if !ok {
		return "", nil, nil, nil, "WINDOW_NOT_FOUND"
	}
	if _, ok := nodeAt(&w.Tree, path); !ok {
		return "", nil, nil, nil, "STALE_REF"
	}
	return snap, path, s, w, ""
}

func (r *run) find(f map[string]string) int {
	var w *Window
	var start []int
	var snap string
	var s *saved
	if root := f["--root"]; root != "" {
		var code string
		snap, start, s, w, code = r.resolve(root)
		if code != "" {
			return r.fail(code, "cannot resolve "+root, "not_delivered", 1)
		}
	} else {
		var ok bool
		if w, ok = r.window(f["--window-id"]); !ok {
			return r.fail("WINDOW_NOT_FOUND", "no window", "not_delivered", 1)
		}
		snap = newSnapshotID()
		s = &saved{Window: w.ID, Refs: map[string][]int{}}
	}
	limit, _ := strconv.Atoi(f["--limit"])
	exact := f["--exact"] == "true"
	like := func(have *string, want string) bool {
		if want == "" {
			return true
		}
		if have == nil {
			return false
		}
		if exact {
			return strings.EqualFold(*have, want)
		}
		return strings.Contains(strings.ToLower(*have), strings.ToLower(want))
	}
	matches := []map[string]any{}
	roles := map[string]bool{}
	var walk func(n *Node, path []int)
	walk = func(n *Node, path []int) {
		roles[n.Role] = true
		hit := (f["--role"] == "" || strings.EqualFold(n.Role, f["--role"])) && like(n.Name, f["--name"]) &&
			like(n.Value, f["--value"]) && (f["--text"] == "" || like(n.Name, f["--text"]) || like(n.Value, f["--text"]))
		if hit && (limit == 0 || len(matches) < limit) {
			m := map[string]any{"role": n.Role}
			if n.Name != nil {
				m["name"] = *n.Name
			}
			if n.Value != nil && !ax.IsSecure(n.Role, n.States) {
				m["value"] = *n.Value
			}
			if len(n.States) > 0 {
				m["states"] = n.States
			}
			if len(n.AvailableActions) > 0 {
				s.Next++
				ref := "@" + snap + ":e" + strconv.Itoa(s.Next)
				s.Refs[ref] = append([]int(nil), path...)
				m["ref_id"] = ref
			} else if n.Bounds != nil {
				m["bounds"] = n.Bounds
			}
			matches = append(matches, m)
		}
		for i := range n.Children {
			walk(&n.Children[i], append(append([]int(nil), path...), i))
		}
	}
	n, _ := nodeAt(&w.Tree, start)
	walk(n, start)
	if err := r.save(snap, s); err != nil {
		return r.fail("INTERNAL", err.Error(), "not_delivered", 1)
	}
	data := map[string]any{"snapshot_id": snap, "matches": matches}
	if len(matches) == 0 && f["--role"] != "" {
		var present []string
		for role := range roles {
			present = append(present, role)
		}
		data["roles_present"] = present
	}
	return r.ok("find", data)
}

func (r *run) get(cmd string, f map[string]string, pos []string) int {
	if len(pos) != 1 {
		return r.fail("INVALID_ARGS", "one ref", "not_delivered", 2)
	}
	ref := pos[0]
	_, path, _, w, code := r.resolve(ref)
	if code != "" {
		return r.fail(code, "cannot resolve "+ref, "not_delivered", 1)
	}
	n, _ := nodeAt(&w.Tree, path)
	prop := f["--property"]
	if cmd == "is" {
		has := false
		for _, s := range n.States {
			has = has || s == prop
		}
		return r.ok(cmd, map[string]any{"ref": ref, "property": prop, "result": has})
	}
	data := map[string]any{"ref": ref, "property": prop}
	switch prop {
	case "role":
		data["value"] = n.Role
	case "title":
		data["value"] = n.Name
	case "states":
		data["value"] = n.States
	case "bounds":
		data["value"], data["live"] = n.Bounds, true
	case "text", "value":
		// The real tool withholds a secure field's value; the fake hands it out, so a test can prove
		// the daemon never asks.
		data["value"] = n.Value
	}
	return r.ok(cmd, data)
}

func (r *run) act(cmd string, pos []string) int {
	if len(pos) < 1 {
		return r.fail("INVALID_ARGS", "a ref", "not_delivered", 2)
	}
	ref := pos[0]
	text := ""
	if len(pos) > 1 {
		text = pos[1]
	}
	_, path, _, w, code := r.resolve(ref)
	if code != "" {
		return r.fail(code, "cannot resolve "+ref, "not_delivered", 1)
	}
	n, _ := nodeAt(&w.Tree, path)
	r.line.Action, r.line.Ref, r.line.Text, r.line.Window = cmd, ref, text, w.ID
	if n.Error != "" {
		delivery := n.Delivery
		if delivery == "" {
			delivery = "not_delivered"
		}
		var details any
		if n.Error == "ACTION_FAILED" && delivery != "not_delivered" {
			details = map[string]any{"kind": "post_action_verification", "post_state": map[string]any{"role": n.Role, "value": n.Value}}
		}
		return r.failWith(n.Error, "the fake refuses", delivery, details, 1)
	}
	delivery := n.Delivery
	if delivery == "" {
		delivery = "delivered_verified"
	}
	post := map[string]any{"role": n.Role, "states": n.States}
	switch cmd {
	case "set-value", "type", "select":
		post["value"] = text
	default:
		if n.Value != nil {
			post["value"] = *n.Value
		}
	}
	return r.ok(cmd, map[string]any{"action": cmd, "post_state": post,
		"disposition": map[string]any{"delivery": delivery, "retry": "unsafe"}})
}
