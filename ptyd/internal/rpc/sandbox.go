package rpc

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"

	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// sandboxParams is `terminal.create {sandbox}`: the folders the program may write. Everything else
// is read-only, and the daemon's own directories are hidden, with whatever else the host names in
// hide: on the operator's machine, the installation's own state, keys and launcher files, which the
// daemon cannot know of and which a shell of the operator's user would otherwise read.
type sandboxParams struct {
	Writable      []string `json:"writable"`
	Hide          []string `json:"hide"`
	ReadIsolation string   `json:"read_isolation"`
}

// MaxHidden is the most paths one terminal's sandbox may be asked to hide.
const MaxHidden = 64

// sandboxRequest reads the create's sandbox, or nil when none was asked for.
func sandboxRequest(raw json.RawMessage) (*sandboxParams, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var p sandboxParams
	if err := decode(raw, &p); err != nil {
		return nil, err
	}
	if len(p.Writable) > sandbox.MaxWritable {
		return nil, wire.Errorf(wire.CodeInvalidParams, "sandbox: at most %d writable folders", sandbox.MaxWritable)
	}
	if len(p.Hide) > MaxHidden {
		return nil, wire.Errorf(wire.CodeInvalidParams, "sandbox: at most %d hidden paths", MaxHidden)
	}
	for _, h := range p.Hide {
		if !filepath.IsAbs(h) {
			return nil, wire.Errorf(wire.CodeInvalidParams, "sandbox: a hidden path is absolute, not %q", h)
		}
	}
	if p.ReadIsolation != "" {
		if p.ReadIsolation != "credentials" {
			return nil, wire.Errorf(wire.CodeInvalidParams, "sandbox: unknown read isolation %q", p.ReadIsolation)
		}
		// The ordinary sandbox mounts / read-only, including every peer login in the shared home.
		// A selected sealed file cannot close that route, and exec.run remains outside the mount.
		return nil, wire.Errorf(wire.CodeUnsupported, "credential read isolation is unavailable: shared home and side-channel programs remain readable")
	}
	return &p, nil
}

// sandboxStatus is the sandbox capability daemon.info reports: "ok", or why not.
func (d *Daemon) sandboxStatus() string {
	if d.Sandbox == nil {
		return "not available in this build"
	}
	return d.Sandbox.Peek()
}

// wrapSandbox returns the command that runs path/argv inside the sandbox. The program gets back the
// paths of the daemon's state directory it needs, because the sandbox hides the rest of it: the
// shell-integration scripts read-only, and for a launch its overlay files and the hook command
// read-only and its dial directory writable.
func (d *Daemon) wrapSandbox(ctx context.Context, p *sandboxParams, path string, argv []string, cwd, launchID string, strict bool) (sandbox.Plan, error) {
	if d.Sandbox == nil {
		return sandbox.Plan{}, wire.Errorf(wire.CodeUnsupported, "the sandbox is not available in this build")
	}
	if status := d.Sandbox.Status(ctx); status != sandbox.OK {
		return sandbox.Plan{}, wire.Errorf(wire.CodeUnsupported, "the sandbox is not available: %s", status)
	}
	var rebind []sandbox.Bind
	// The shell-integration scripts live in the state directory too: without them a sandboxed shell
	// would start without its command marks (bash's --init-file would name a missing file).
	if d.ShellDir != "" {
		rebind = append(rebind, sandbox.Bind{Path: d.ShellDir})
	}
	if d.Side != nil && launchID != "" {
		if paths, ok := d.Side.Launches.Paths(launchID); ok {
			rebind = append(rebind, sandbox.Bind{Path: paths.Dir}, sandbox.Bind{Path: paths.Dial, Writable: true}, sandbox.Bind{Path: paths.Bin}, sandbox.Bind{Path: paths.Exe})
		}
	}
	program := append([]string{path}, argv[1:]...)
	masks, covers := hidden(p.Hide)
	plan, err := sandbox.Wrap(sandbox.Options{
		Bwrap: d.Sandbox.Bwrap(), Argv: program, Cwd: cwd, Writable: p.Writable,
		Mask: append([]string{d.Config.RunDir, d.Config.StateDir}, masks...), Cover: covers, Rebind: rebind, Strict: strict,
	})
	if errors.Is(err, sandbox.ErrHiddenCwd) {
		return sandbox.Plan{}, wire.Errorf(wire.CodeInvalidParams, "%v", err)
	}
	if err != nil {
		return sandbox.Plan{}, wire.Errorf(wire.CodeInternal, "%v", err)
	}
	return plan, nil
}

// hidden sorts the host's hidden paths into folders to mask and files to cover, each where it really
// is: bubblewrap cannot mount over a symbolic link and refuses the whole command, and a cover on the
// link would leave its target readable. A path that is not there is skipped, for the same reason.
func hidden(paths []string) (folders, files []string) {
	for _, raw := range paths {
		real, err := filepath.EvalSymlinks(filepath.Clean(raw))
		if err != nil {
			continue
		}
		st, err := os.Stat(real)
		switch {
		case err != nil:
		case st.IsDir():
			folders = append(folders, real)
		case st.Mode().IsRegular():
			files = append(files, real)
		}
	}
	return folders, files
}
