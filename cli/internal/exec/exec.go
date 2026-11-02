// Package exec is the local executor: the shell and file calls the server sends down the device
// connection, run on this machine inside the folder each session is bound to.
//
// The handlers are ptyd's (github.com/ascorblack/daedalus/ptyd/sidechan): the same exec.run, with its
// process groups, timeouts and output caps, and the same fs.stat, fs.list and fs.read, with their deny
// list and their checks against a symbolic link swapped in between the check and the open. What is
// this package's own is the jail around them (one folder per session, as written and as resolved) and
// the general fs.write and fs.mkdir that ptyd, which only writes into a staff inbox, does not have.
package exec

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/sidechan"
)

// Error codes, ptyd's and JSON-RPC's (docs/architecture/terminals.md). The numbers are the contract.
const (
	CodeInvalidParams  = -32602
	CodeMethodNotFound = -32601
	CodeInternal       = -32603
	CodeNotFound       = 1001
	CodeLimit          = 1003
	CodeForbidden      = 1004
	CodeTimeout        = 1005
	CodeUnsupported    = 1007
)

// Refusal reasons carried in a 1004's data.
const (
	ReasonOperator    = "refused_by_operator"
	ReasonPlan        = "plan_guard"
	ReasonOutsideRoot = "outside_root"
	ReasonDenied      = "denied_path"
)

// Error is a call's failure as the device connection reports it.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func (e *Error) Error() string { return fmt.Sprintf("%s (%d)", e.Message, e.Code) }

// Refuse is a 1004 with a reason, the form every refusal takes.
func Refuse(reason, message string) *Error {
	data := map[string]string{"reason": reason}
	if message != "" {
		data["message"] = message
	}
	return &Error{Code: CodeForbidden, Message: message, Data: data}
}

// maxWritePiece bounds one fs.write piece: a relay message is at most 1 MiB, and base64 makes three
// bytes four.
const maxWritePiece = 512 << 10

// maxWriteFile bounds a file written through fs.write, as ptyd bounds its own.
const maxWriteFile = 50 << 20

// FSMethods are the native file calls this executor serves, as device.hello advertises them.
var FSMethods = []string{"stat", "list", "read", "write", "mkdir", "find", "search"}

// Executor runs calls for the sessions bound to this machine.
type Executor struct {
	exec *sidechan.Exec
	fs   *sidechan.FS
	home string

	mu    sync.Mutex
	roots map[string]string
	// writing serialises the pieces of writes, so two pieces of one file cannot interleave.
	writing sync.Mutex
}

// New returns an executor for this user, with no session bound yet.
func New(log *slog.Logger) (*Executor, error) {
	if log == nil {
		log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("find the home folder: %w", err)
	}
	fs, err := sidechan.NewFS(nil, nil, nil, home)
	if err != nil {
		return nil, err
	}
	return &Executor{exec: sidechan.NewOpenExec(os.Environ(), home, log), fs: fs, home: home, roots: map[string]string{}}, nil
}

// CheckRoot says why a folder cannot be a session's root, or nil when it can: it must be an
// absolute, existing directory, and neither the filesystem's root nor the home folder nor anything
// above it, because the deny list names the credentials it knows of and a jail that wide would put
// every one it does not know of within reach.
func (e *Executor) CheckRoot(root string) error {
	if !filepath.IsAbs(root) {
		return fmt.Errorf("%s is not an absolute path", root)
	}
	st, err := os.Stat(root)
	if err != nil {
		return err
	}
	if !st.IsDir() {
		return fmt.Errorf("%s is not a folder", root)
	}
	_, refused, err := e.probeRoot(root)
	if err != nil {
		return err
	}
	if refused != "" {
		return errors.New(refused)
	}
	return nil
}

// probeRoot asks ptyd's own root rule about one folder, on a scratch reader so the live roots are
// untouched.
func (e *Executor) probeRoot(root string) (string, string, error) {
	probe, err := sidechan.NewFS(nil, nil, nil, e.home)
	if err != nil {
		return "", "", err
	}
	accepted, refused, err := probe.SetRoots([]string{root})
	if err != nil {
		return "", "", err
	}
	if len(refused) > 0 {
		return "", fmt.Sprintf("%s cannot hold a session's tools: %s", root, refused[0].Reason), nil
	}
	return accepted[0], "", nil
}

// Bind makes root the jail of a session's calls.
func (e *Executor) Bind(sessionID, root string) error {
	if err := e.CheckRoot(root); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.roots[sessionID] = filepath.Clean(root)
	return e.syncRoots()
}

// Unbind forgets a session.
func (e *Executor) Unbind(sessionID string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.roots, sessionID)
	_ = e.syncRoots()
}

// Root is a session's jail.
func (e *Executor) Root(sessionID string) (string, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	r, ok := e.roots[sessionID]
	return r, ok
}

// syncRoots hands ptyd's reader the union of the jails; the per-session check is made here first.
func (e *Executor) syncRoots() error {
	roots := make([]string, 0, len(e.roots))
	for _, r := range e.roots {
		roots = append(roots, r)
	}
	_, _, err := e.fs.SetRoots(roots)
	return err
}

// Request is one call, decoded far enough for the jail.
type Request struct {
	Method string
	Params json.RawMessage
	// Session is _meta.session_id: whose jail applies.
	Session string
	// Output, for exec.run, receives the command's output as it is written (see sidechan.ExecRequest).
	Output io.Writer
}

// Handle runs one call. The error is always an *Error.
func (e *Executor) Handle(ctx context.Context, r Request) (any, error) {
	root, ok := e.Root(r.Session)
	if !ok {
		return nil, Refuse(ReasonOutsideRoot, "this terminal holds no folder for session "+r.Session)
	}
	switch r.Method {
	case "exec.run":
		return e.execRun(ctx, root, r.Params, r.Output)
	case "fs.stat":
		var p struct {
			Path string `json:"path"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		if err := e.jail(root, p.Path); err != nil {
			return nil, err
		}
		st, err := e.fs.Stat(p.Path)
		return st, sideError(err)
	case "fs.list":
		var p struct {
			Path  string `json:"path"`
			Glob  string `json:"glob"`
			Sort  string `json:"sort"`
			Limit int    `json:"limit"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		if err := e.jail(root, p.Path); err != nil {
			return nil, err
		}
		entries, truncated, err := e.fs.List(p.Path, p.Glob, p.Sort, p.Limit)
		if err != nil {
			return nil, sideError(err)
		}
		if entries == nil {
			entries = []sidechan.Entry{}
		}
		return map[string]any{"entries": entries, "truncated": truncated}, nil
	case "fs.read":
		var p struct {
			Path   string `json:"path"`
			Offset int64  `json:"offset"`
			Max    int    `json:"max"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		if err := e.jail(root, p.Path); err != nil {
			return nil, err
		}
		res, err := e.fs.Read(p.Path, p.Offset, p.Max)
		return res, sideError(err)
	case "fs.write":
		var p struct {
			Path   string `json:"path"`
			Offset int64  `json:"offset"`
			Data   []byte `json:"data_b64"`
			Final  bool   `json:"final"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		return e.write(root, p.Path, p.Offset, p.Data, p.Final)
	case "fs.find":
		var p struct {
			Path    string `json:"path"`
			Pattern string `json:"pattern"`
			Limit   int    `json:"limit"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		return e.find(ctx, root, p.Path, p.Pattern, p.Limit)
	case "fs.search":
		var p struct {
			Path            string `json:"path"`
			Pattern         string `json:"pattern"`
			Glob            string `json:"glob"`
			CaseInsensitive bool   `json:"case_insensitive"`
			Limit           int    `json:"limit"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		return e.search(ctx, root, p.Path, p.Pattern, p.Glob, p.CaseInsensitive, p.Limit)
	case "fs.mkdir":
		var p struct {
			Path string `json:"path"`
		}
		if err := decode(r.Params, &p); err != nil {
			return nil, err
		}
		return e.mkdir(root, p.Path)
	}
	return nil, &Error{Code: CodeMethodNotFound, Message: "this terminal does not serve " + r.Method}
}

func (e *Executor) execRun(ctx context.Context, root string, raw json.RawMessage, output io.Writer) (any, error) {
	var p struct {
		Argv      []string          `json:"argv"`
		Cwd       string            `json:"cwd"`
		Env       map[string]string `json:"env"`
		TimeoutMs int64             `json:"timeout_ms"`
		Stdin     []byte            `json:"stdin_b64"`
		MaxOutput int               `json:"max_output"`
	}
	if err := decode(raw, &p); err != nil {
		return nil, err
	}
	if p.TimeoutMs < 0 {
		return nil, &Error{Code: CodeInvalidParams, Message: "timeout_ms must not be negative"}
	}
	if p.Cwd == "" {
		p.Cwd = root
	}
	if err := e.jail(root, p.Cwd); err != nil {
		return nil, err
	}
	res, err := e.exec.Run(ctx, sidechan.ExecRequest{Argv: p.Argv, Cwd: p.Cwd, Env: p.Env,
		Timeout: time.Duration(p.TimeoutMs) * time.Millisecond, Stdin: p.Stdin, MaxOutput: p.MaxOutput, Output: output})
	if err != nil {
		return nil, sideError(err)
	}
	return res, nil
}

// jail refuses a path outside the session's folder, as written or as the filesystem resolves it, and
// a path on the deny list.
func (e *Executor) jail(root, p string) error {
	if !filepath.IsAbs(p) {
		return &Error{Code: CodeInvalidParams, Message: "the path must be absolute: " + p}
	}
	clean := filepath.Clean(p)
	if !under(root, clean) {
		return Refuse(ReasonOutsideRoot, clean+" is outside "+root)
	}
	rootReal := root
	if r, err := filepath.EvalSymlinks(root); err == nil {
		rootReal = r
	}
	if real := resolve(clean); !under(rootReal, real) {
		return Refuse(ReasonOutsideRoot, clean+" resolves to "+real+", outside "+root)
	}
	if e.fs.Refused(clean) {
		return Refuse(ReasonDenied, clean+" is on the deny list")
	}
	return nil
}

// resolve is the path as the filesystem resolves it: its nearest existing ancestor resolved, with the
// rest appended, so a missing file inside a symbolic link to elsewhere still resolves to elsewhere.
func resolve(p string) string {
	rest := ""
	cur := p
	for {
		if real, err := filepath.EvalSymlinks(cur); err == nil {
			if rest == "" {
				return real
			}
			return filepath.Join(real, rest)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return p
		}
		if rest == "" {
			rest = filepath.Base(cur)
		} else {
			rest = filepath.Join(filepath.Base(cur), rest)
		}
		cur = parent
	}
}

// under reports whether p is dir or inside it, by the platform's case rule.
func under(dir, p string) bool {
	if runtime.GOOS == "windows" {
		dir, p = strings.ToLower(dir), strings.ToLower(p)
	}
	if strings.HasSuffix(dir, string(filepath.Separator)) {
		return strings.HasPrefix(p, dir)
	}
	return p == dir || strings.HasPrefix(p, dir+string(filepath.Separator))
}

// write is fs.write: offset 0 starts a temporary file beside the target, later pieces continue it,
// and the piece marked final renames it over the target, so a write cut off part way leaves the old
// file whole instead of a half-written one under its name.
func (e *Executor) write(root, p string, offset int64, data []byte, final bool) (any, error) {
	if len(data) > maxWritePiece {
		return nil, &Error{Code: CodeLimit, Message: fmt.Sprintf("one write carries at most %d bytes", maxWritePiece)}
	}
	if offset < 0 || offset+int64(len(data)) > maxWriteFile {
		return nil, &Error{Code: CodeLimit, Message: fmt.Sprintf("a file written here is at most %d bytes", maxWriteFile)}
	}
	if err := e.jail(root, p); err != nil {
		return nil, err
	}
	clean := filepath.Clean(p)
	if clean == filepath.Clean(root) {
		return nil, &Error{Code: CodeInvalidParams, Message: "the session's folder itself is not a file"}
	}
	e.writing.Lock()
	defer e.writing.Unlock()
	dir, name := filepath.Dir(clean), filepath.Base(clean)
	if err := os.MkdirAll(dir, 0o777); err != nil {
		return nil, osError(err)
	}
	// Made, the folder is checked again: a link swapped in for a missing folder between the first
	// check and the mkdir would otherwise be written through.
	if err := e.jail(root, dir); err != nil {
		return nil, err
	}
	tmp := filepath.Join(dir, "."+name+".daedalus-cli.tmp")
	var f *os.File
	var err error
	if offset == 0 {
		f, err = os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o666)
	} else {
		f, err = os.OpenFile(tmp, os.O_WRONLY, 0)
		if errors.Is(err, os.ErrNotExist) {
			return nil, &Error{Code: CodeInvalidParams, Message: "a write continued at offset " + fmt.Sprint(offset) + " that never started"}
		}
	}
	if err != nil {
		return nil, osError(err)
	}
	st, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return nil, osError(err)
	}
	if st.Size() != offset {
		_ = f.Close()
		return nil, &Error{Code: CodeInvalidParams, Message: fmt.Sprintf("the write is at %d bytes, not %d", st.Size(), offset)}
	}
	if _, err := f.WriteAt(data, offset); err != nil {
		_ = f.Close()
		return nil, osError(err)
	}
	if err := f.Close(); err != nil {
		return nil, osError(err)
	}
	size := offset + int64(len(data))
	before, statErr := os.Lstat(clean)
	created := errors.Is(statErr, os.ErrNotExist)
	if !final {
		return map[string]any{"size": size, "created": created}, nil
	}
	if before != nil {
		if before.IsDir() {
			_ = os.Remove(tmp)
			return nil, &Error{Code: CodeInvalidParams, Message: clean + " is a folder"}
		}
		// The new content keeps the old file's permissions: an executable script stays one.
		_ = os.Chmod(tmp, before.Mode().Perm())
	}
	if err := os.Rename(tmp, clean); err != nil {
		_ = os.Remove(tmp)
		return nil, osError(err)
	}
	return map[string]any{"size": size, "created": created}, nil
}

func (e *Executor) mkdir(root, p string) (any, error) {
	if err := e.jail(root, p); err != nil {
		return nil, err
	}
	clean := filepath.Clean(p)
	_, statErr := os.Stat(clean)
	created := errors.Is(statErr, os.ErrNotExist)
	if err := os.MkdirAll(clean, 0o777); err != nil {
		return nil, osError(err)
	}
	if err := e.jail(root, clean); err != nil {
		return nil, err
	}
	return map[string]any{"created": created}, nil
}

func decode(raw json.RawMessage, into any) error {
	if len(raw) == 0 {
		raw = json.RawMessage("{}")
	}
	if err := json.Unmarshal(raw, into); err != nil {
		return &Error{Code: CodeInvalidParams, Message: "the parameters do not parse: " + err.Error()}
	}
	return nil
}

// sideError gives ptyd's errors their protocol codes, as ptyd's own dispatcher does.
func sideError(err error) error {
	if err == nil {
		return nil
	}
	var own *Error
	switch {
	case errors.As(err, &own):
		return err
	case errors.Is(err, sidechan.ErrInvalid):
		return &Error{Code: CodeInvalidParams, Message: err.Error()}
	case errors.Is(err, sidechan.ErrForbidden):
		return Refuse(ReasonDenied, err.Error())
	case errors.Is(err, sidechan.ErrNotFound), errors.Is(err, os.ErrNotExist):
		return &Error{Code: CodeNotFound, Message: err.Error()}
	case errors.Is(err, sidechan.ErrBusy):
		return &Error{Code: CodeLimit, Message: err.Error()}
	}
	return &Error{Code: CodeInternal, Message: err.Error()}
}

func osError(err error) error {
	if errors.Is(err, os.ErrNotExist) {
		return &Error{Code: CodeNotFound, Message: err.Error()}
	}
	if errors.Is(err, os.ErrPermission) {
		return &Error{Code: CodeForbidden, Message: err.Error(), Data: map[string]string{"reason": "permission"}}
	}
	return &Error{Code: CodeInternal, Message: err.Error()}
}

// Shell runs a command the operator typed in `!` mode, in dir. It is not a call from the server, so
// neither the guard nor a session's jail applies: the operator is the one asking.
func (e *Executor) Shell(ctx context.Context, dir, command string, timeout time.Duration) (sidechan.ExecResult, error) {
	argv := []string{"bash", "-c", command}
	if runtime.GOOS == "windows" {
		argv = []string{"powershell", "-NoProfile", "-Command", command}
	} else if _, err := os.Stat("/bin/bash"); err != nil {
		argv[0] = "sh"
	}
	return e.exec.Run(ctx, sidechan.ExecRequest{Argv: argv, Cwd: dir, Timeout: timeout})
}
