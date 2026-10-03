package rpc

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/containment"
	"github.com/ascorblack/daedalus/ptyd/internal/ptyproc"
	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/internal/shellint"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// validID is what a terminal id may look like. The host generates ids; the daemon only makes sure
// one is safe to use as a file name for the terminal's log.
var validID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

const (
	maxLabels     = 32
	maxLabelBytes = 256
	maxArgs       = 1024
	maxEnvVars    = 1024
)

type createParams struct {
	ID    string   `json:"id"`
	Argv  []string `json:"argv"`
	Shell *struct {
		Program     string `json:"program"`
		Login       *bool  `json:"login"`
		Integration *bool  `json:"integration"`
	} `json:"shell"`
	Cwd         string            `json:"cwd"`
	Env         map[string]string `json:"env"`
	StripEnv    []string          `json:"strip_env"`
	Cols        int               `json:"cols"`
	Rows        int               `json:"rows"`
	Title       string            `json:"title"`
	RingBytes   int               `json:"ring_bytes"`
	LogToDisk   bool              `json:"log_to_disk"`
	InputIdleMs *int64            `json:"input_idle_ms"`
	Sandbox     json.RawMessage   `json:"sandbox"`
	LaunchID    string            `json:"launch_id"`
	Labels      map[string]string `json:"labels"`
	Resources   *struct {
		Scope            containment.Scope  `json:"scope"`
		Limits           containment.Limits `json:"limits"`
		ExpectedInstance string             `json:"expected_instance"`
	} `json:"resources"`
}

// checkSize refuses a size outside the daemon's range. A size below the minimum is what a hidden
// browser pane measures itself at; applied, it wraps every line of a live program at nine columns.
func checkSize(cols, rows int) error {
	if cols < config.MinCols || rows < config.MinRows || cols > config.MaxCols || rows > config.MaxRows {
		return &wire.Error{Code: wire.CodeInvalidSize, Message: "size outside the allowed range",
			Data: map[string]int{"cols": cols, "rows": rows, "min_cols": config.MinCols, "min_rows": config.MinRows,
				"max_cols": config.MaxCols, "max_rows": config.MaxRows}}
	}
	return nil
}

func (d *Daemon) create(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p createParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	if !validID.MatchString(p.ID) {
		return nil, bad("id must be 1-64 letters, digits, '-' or '_'")
	}
	box, err := sandboxRequest(p.Sandbox)
	if err != nil {
		return nil, err
	}
	if len(p.Argv) > 0 && p.Shell != nil {
		return nil, bad("give argv or shell, not both")
	}
	if len(p.Argv) > maxArgs || len(p.Env) > maxEnvVars || len(p.Labels) > maxLabels {
		return nil, bad("too many arguments, environment variables or labels")
	}
	for k, v := range p.Labels {
		if len(k) > maxLabelBytes || len(v) > maxLabelBytes {
			return nil, bad("label %q is longer than %d bytes", k[:min(len(k), 32)], maxLabelBytes)
		}
	}
	if p.Cols == 0 && p.Rows == 0 {
		p.Cols, p.Rows = config.DefaultCols, config.DefaultRows
	}
	if err := checkSize(p.Cols, p.Rows); err != nil {
		return nil, err
	}
	ring := d.Config.Limits.RingBytes
	if p.RingBytes != 0 {
		if p.RingBytes < config.MinRingBytes || p.RingBytes > config.MaxRingBytes {
			return nil, bad("ring_bytes must be %d..%d", config.MinRingBytes, config.MaxRingBytes)
		}
		ring = p.RingBytes
	}
	idle := d.Config.Limits.InputIdle
	if p.InputIdleMs != nil {
		if *p.InputIdleMs < 0 || *p.InputIdleMs > 600000 {
			return nil, bad("input_idle_ms must be 0..600000")
		}
		idle = time.Duration(*p.InputIdleMs) * time.Millisecond
	}
	for k := range p.Env {
		if k == "" || containsAny(k, "=\x00") {
			return nil, bad("environment name %q", k)
		}
	}

	// The working directory: absolute, and existing. A missing one falls back to home rather than
	// failing, because the folder of a session may be deleted while its terminal is being reopened;
	// the reply says so and the host shows it.
	cwd, fallback := p.Cwd, false
	if cwd == "" {
		cwd = d.Config.Home
	} else if !filepath.IsAbs(cwd) {
		return nil, bad("cwd must be absolute")
	}
	if st, err := os.Stat(cwd); err != nil || !st.IsDir() {
		cwd, fallback = d.Config.Home, true
	}

	extra := map[string]string{"HOME": d.Config.Home}
	for k, v := range p.Env {
		extra[k] = v
	}
	term.SetPWD(extra, cwd)
	if _, set := extra["HISTFILE"]; box != nil && !set {
		extra["HISTFILE"] = sandbox.HistoryFile
	}
	// The launch's variables go last: the host's own env cannot replace a launch's token or URL.
	if err := d.launchEnv(p.LaunchID, extra); err != nil {
		return nil, err
	}
	// Every terminal gets a nonce, not only an integrated shell: a program that prints shell marks
	// itself (a harness, a script) is believed when it knows the nonce of the terminal it runs in.
	nonce := shellint.NewNonce()
	extra[shellint.NonceEnv] = nonce
	env := term.BuildEnv(d.Environ, p.StripEnv, extra, p.ID)

	argv := p.Argv
	shell, integration := "", ""
	if len(argv) == 0 {
		shell = d.Config.Shell
		login, integrate := true, true
		if p.Shell != nil {
			if p.Shell.Program != "" {
				shell = p.Shell.Program
			}
			if p.Shell.Login != nil {
				login = *p.Shell.Login
			}
			if p.Shell.Integration != nil {
				integrate = *p.Shell.Integration
			}
		}
		argv = shellint.PlainArgs(shell, login, runtime.GOOS)
		if kind := shellint.KindOf(shell); integrate && kind != "" && d.ShellDir != "" {
			userZdotdir, hasZdotdir := lookupEnv(env, "ZDOTDIR")
			if l, ok := shellint.Rewrite(kind, shell, login, d.ShellDir, nonce, userZdotdir, hasZdotdir); ok {
				argv, integration = l.Argv, kind
				for k, v := range l.Env {
					extra[k] = v
				}
				env = term.BuildEnv(d.Environ, p.StripEnv, extra, p.ID)
			}
		}
	}
	path, ok := term.LookPath(argv[0], env, cwd)
	if !ok {
		return nil, bad("%q is not an executable on the terminal's PATH", argv[0])
	}
	var program []string
	var plan sandbox.Plan
	if box != nil {
		if plan, err = d.wrapSandbox(ctx, box, path, argv, cwd, p.LaunchID, p.Resources != nil); err != nil {
			return nil, err
		}
		program, argv, path = argv, plan.Argv, plan.Argv[0]
	}
	logPath := ""
	if p.LogToDisk {
		logPath = filepath.Join(d.Config.StateDir, "terminals", p.ID+".log")
	}
	labels := p.Labels
	if labels == nil {
		labels = map[string]string{}
	}
	var owned containment.Handle
	if p.Resources != nil {
		if box == nil {
			return nil, wire.Errorf(wire.CodeForbidden, "strict attempt containment needs a read-only system mount")
		}
		if d.Containment == nil {
			return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment is unavailable: %s", d.ContainmentReason)
		}
		if p.Resources.Scope.LaunchID != p.LaunchID || p.LaunchID == "" {
			return nil, bad("attempt containment must bind this exact launch")
		}
		if p.Resources.ExpectedInstance != d.Instance {
			return nil, wire.Errorf(wire.CodeUnsupported, "the daemon generation changed before process placement")
		}
		if status := d.Containment.Probe(); !status.Available {
			return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment is unavailable: %s", status.Reason)
		}
		owned, err = d.Containment.Claim(p.Resources.Scope, p.Resources.Limits)
		if err != nil {
			return nil, wire.Errorf(wire.CodeUnsupported, "attempt containment: %v", err)
		}
	}
	t, err := d.Registry.Create(term.Spec{
		ID: p.ID, Path: path, Argv: argv, Cwd: cwd, CwdFallback: fallback, Env: env, Cols: p.Cols, Rows: p.Rows,
		Title: p.Title, RingBytes: ring, LogPath: logPath, InputIdle: idle, LaunchID: p.LaunchID, Labels: labels,
		Shell: shell, Nonce: nonce, Integration: integration, Sandbox: box != nil, Program: program,
		Containment: owned,
	})
	d.launchStarted(t, p.LaunchID, p.ID)
	if err != nil {
		if owned != nil {
			_ = owned.Close()
		}
		if e := termError(err); e != err {
			return nil, e
		}
		return nil, wire.Errorf(wire.CodeInternal, "starting %s: %v", argv[0], err)
	}
	name := filepath.Base(path)
	if program != nil {
		name = filepath.Base(program[0])
	}
	d.Log.Info("terminal created", "terminal", t.ID, "pid", t.Pid, "program", name, "sandbox", box != nil)
	reply := map[string]any{"id": t.ID, "pid": t.Pid, "cwd": cwd, "cwd_fallback": fallback, "shell": shell,
		"shell_integration": integration, "created_at": t.CreatedAt}
	if owned != nil {
		observed, observeErr := owned.Observe()
		if observeErr != nil {
			return nil, wire.Errorf(wire.CodeInternal, "attempt containment could not be observed: %v", observeErr)
		}
		reply["containment"] = observed
	}
	if box != nil {
		// What the sandbox made writable, and each folder it left read-only with the reason: the host
		// tells the operator rather than letting a write fail later with no explanation.
		skipped := plan.Skipped
		if skipped == nil {
			skipped = []sandbox.Skip{}
		}
		reply["sandbox"] = map[string]any{"writable": plan.Writable, "skipped": skipped}
	}
	return reply, nil
}

// lookupEnv finds a variable in an environment list; the last assignment wins, as for a process.
func lookupEnv(env []string, name string) (string, bool) {
	value, found := "", false
	for _, kv := range env {
		if v, ok := strings.CutPrefix(kv, name+"="); ok {
			value, found = v, true
		}
	}
	return value, found
}

func containsAny(s, chars string) bool {
	for i := 0; i < len(s); i++ {
		for j := 0; j < len(chars); j++ {
			if s[i] == chars[j] {
				return true
			}
		}
	}
	return false
}

func (d *Daemon) list(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		IDs         []string `json:"ids"`
		PreviewRows int      `json:"preview_rows"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.PreviewRows < 0 || p.PreviewRows > 12 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "preview_rows must be 0..12")
	}
	want := map[string]bool{}
	for _, id := range p.IDs {
		want[id] = true
	}
	out := []term.Info{}
	for _, t := range d.Registry.List() {
		if len(want) > 0 && !want[t.ID] {
			continue
		}
		info := t.Info()
		if p.PreviewRows > 0 {
			if rows := t.Preview(p.PreviewRows); rows != nil {
				info.Preview = rows
			}
		}
		out = append(out, info)
	}
	return map[string]any{"terminals": out}, nil
}

type idParams struct {
	ID string `json:"id"`
}

func (d *Daemon) terminal(params json.RawMessage, v any, id *string) (*term.Terminal, error) {
	if err := decode(params, v); err != nil {
		return nil, err
	}
	t, err := d.Registry.Get(*id)
	return t, termError(err)
}

func (d *Daemon) get(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p idParams
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	return t.Info(), nil
}

type writeParams struct {
	ID       string   `json:"id"`
	Text     *string  `json:"text"`
	Paste    *string  `json:"paste"`
	Keys     []string `json:"keys"`
	BytesB64 *string  `json:"bytes_b64"`
	Origin   struct {
		Kind     string `json:"kind"`
		Actor    string `json:"actor"`
		LaunchID string `json:"launch_id"`
		Note     string `json:"note"`
	} `json:"origin"`
	Wait      string `json:"wait"`
	TimeoutMs *int64 `json:"timeout_ms"`
}

// defaultWriteTimeout is how long an agent write waits for the keyboard when the caller does not
// say: long enough to outlast a burst of typing, short enough that a forgotten human grant surfaces.
const defaultWriteTimeout = 30 * time.Second

func (d *Daemon) write(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p writeParams
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	bad := func(format string, args ...any) error { return wire.Errorf(wire.CodeInvalidParams, format, args...) }
	given := 0
	for _, set := range []bool{p.Text != nil, p.Paste != nil, p.Keys != nil, p.BytesB64 != nil} {
		if set {
			given++
		}
	}
	if given != 1 {
		return nil, bad("give exactly one of text, paste, keys and bytes_b64")
	}
	if p.Origin.Kind != "agent" || p.Origin.Actor == "" {
		// Human input arrives on an attachment, never through this method; every write here is an
		// agent's, and the journal must be able to say whose.
		return nil, bad("origin must be {kind: \"agent\", actor: …}")
	}
	wait := true
	switch p.Wait {
	case "", "keyboard":
	case "none":
		wait = false
	default:
		return nil, bad("wait must be keyboard or none")
	}
	timeout := defaultWriteTimeout
	if p.TimeoutMs != nil {
		timeout = time.Duration(*p.TimeoutMs) * time.Millisecond
		if timeout < 0 || timeout > config.MaxWriteTimeout {
			return nil, bad("timeout_ms must be 0..%d", config.MaxWriteTimeout.Milliseconds())
		}
	}
	if !t.Running() {
		return nil, termError(term.ErrExited)
	}
	var data []byte
	kind := ""
	switch {
	case p.Text != nil:
		kind, data = "text", []byte(*p.Text)
	case p.Paste != nil:
		kind, data = "paste", term.EncodePaste(*p.Paste, t.Modes())
	case p.Keys != nil:
		kind = "keys"
		if data, err = term.EncodeKeys(p.Keys, t.Modes()); err != nil {
			return nil, bad("%v", err)
		}
	default:
		kind = "bytes"
		if data, err = base64.StdEncoding.DecodeString(*p.BytesB64); err != nil {
			return nil, bad("bytes_b64: %v", err)
		}
	}
	if len(data) > config.MaxWriteBytes {
		return nil, wire.Errorf(wire.CodeLimit, "a write is at most %d bytes; put longer text in a file", config.MaxWriteBytes)
	}
	receipt, err := t.AgentWrite(ctx, kind, data, term.Origin{Actor: p.Origin.Actor, LaunchID: p.Origin.LaunchID,
		Note: p.Origin.Note}, wait, timeout)
	if err != nil {
		return nil, termError(err)
	}
	return receipt, nil
}

func (d *Daemon) resize(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID   string `json:"id"`
		Cols int    `json:"cols"`
		Rows int    `json:"rows"`
		PxW  int    `json:"px_w"`
		PxH  int    `json:"px_h"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	if err := checkSize(p.Cols, p.Rows); err != nil {
		return nil, err
	}
	if p.PxW < 0 || p.PxH < 0 || p.PxW > 65535 || p.PxH > 65535 {
		return nil, wire.Errorf(wire.CodeInvalidParams, "pixel sizes must be 0..65535")
	}
	if err := t.Resize(p.Cols, p.Rows, p.PxW, p.PxH); err != nil {
		return nil, termError(err)
	}
	return map[string]any{"cols": p.Cols, "rows": p.Rows}, nil
}

func (d *Daemon) signal(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID     string `json:"id"`
		Signal string `json:"signal"`
		Group  *bool  `json:"group"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	sig, ok := ptyproc.ParseSignal(p.Signal)
	if !ok {
		return nil, wire.Errorf(wire.CodeInvalidParams, "unknown signal %q", p.Signal)
	}
	group := p.Group == nil || *p.Group
	if err := t.Signal(sig, group); err != nil {
		return nil, termError(err)
	}
	return map[string]any{}, nil
}

func (d *Daemon) kill(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID      string `json:"id"`
		GraceMs *int64 `json:"grace_ms"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	grace := d.Config.Limits.KillGrace
	if p.GraceMs != nil {
		grace = time.Duration(*p.GraceMs) * time.Millisecond
		if grace < 0 || grace > config.MaxKillGrace {
			return nil, wire.Errorf(wire.CodeInvalidParams, "grace_ms must be 0..%d", config.MaxKillGrace.Milliseconds())
		}
	}
	exit := t.Kill(grace)
	var sig any
	if exit.Signal != "" {
		sig = exit.Signal
	}
	return map[string]any{"exit_code": exit.Code, "signal": sig}, nil
}

func (d *Daemon) forget(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p idParams
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := d.Registry.Forget(p.ID); err != nil {
		return nil, termError(err)
	}
	return map[string]any{}, nil
}

// readOutputCap keeps a read_output reply inside one frame whatever its encoding costs: base64 is
// four bytes for three, and JSON escaping of stripped text is at most six bytes for one, but text
// that is mostly escapes is not what agents read, so the stripped cap is generous and the reply is
// still checked against the frame size on the way out.
const (
	readOutputRawCap   = 512 << 10
	readOutputStripCap = 768 << 10
)

func (d *Daemon) readOutput(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		ID       string `json:"id"`
		SinceSeq int64  `json:"since_seq"`
		MaxBytes int    `json:"max_bytes"`
		Strip    bool   `json:"strip"`
	}
	t, err := d.terminal(params, &p, &p.ID)
	if err != nil {
		return nil, err
	}
	if p.MaxBytes == 0 {
		p.MaxBytes = 64 << 10
	}
	if p.MaxBytes < 0 || p.MaxBytes > config.MaxReadOutputBytes {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max_bytes must be 1..%d", config.MaxReadOutputBytes)
	}
	limit := min(p.MaxBytes, readOutputRawCap)
	if p.Strip {
		limit = min(p.MaxBytes, readOutputStripCap)
	}
	data, from, to, gap := t.ReadOutput(p.SinceSeq, limit, p.Strip)
	out := map[string]any{"from_seq": from, "to_seq": to, "gap": gap, "head_seq": t.OutputHead()}
	if p.Strip {
		out["data"] = string(data)
	} else {
		out["data_b64"] = base64.StdEncoding.EncodeToString(data)
	}
	return out, nil
}
