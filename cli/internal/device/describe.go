package device

import (
	"encoding/json"
	"strings"

	"github.com/ascorblack/daedalus/cli/internal/guard"
)

// serverPrelude is what the server writes before every shell command it sends (daedalus/host/
// host_exec.py PRELUDE): the login profile, silenced, and stderr folded into stdout. It sources a
// file, which no read-only check can vouch for, so the guard judges the command after it instead. A
// server that writes another prelude only makes the guard more careful, never less.
const serverPrelude = "for profile in \"$HOME/.bash_profile\" \"$HOME/.bash_login\" \"$HOME/.profile\"; do\n" +
	"  if [ -r \"$profile\" ]; then . \"$profile\" >/dev/null 2>&1 </dev/null; break; fi\n" +
	"done\n" +
	"exec 2>&1\n"

// meta is the _meta every request carries.
type meta struct {
	SessionID  string `json:"session_id"`
	ToolCallID string `json:"tool_call_id"`
	Tool       string `json:"tool"`
	Intent     string `json:"intent"`
	Title      string `json:"title"`
}

// Describe is a request as the guard and the person see it. The server's intent is shown but not
// trusted: plan mode lets a command through only when the client itself reads it as read-only, since
// the guard is the operator's rule and the server is the party it restrains.
func Describe(method string, params json.RawMessage) guard.Action {
	var p struct {
		Meta meta     `json:"_meta"`
		Argv []string `json:"argv"`
		Path string   `json:"path"`
	}
	_ = json.Unmarshal(params, &p)
	a := guard.Action{Method: method, Path: p.Path, Session: p.Meta.SessionID}
	switch method {
	case "exec.run":
		a.Kind = guard.Exec
		a.Command = Script(p.Argv)
	case "fs.write", "fs.mkdir":
		a.Kind = guard.Write
	default:
		a.Kind = guard.Read
	}
	return a
}

// Script is the command an argv runs: the script of `bash -c` or `powershell -Command` without the
// server's prelude, or the argv joined.
func Script(argv []string) string {
	if len(argv) >= 3 {
		prog := strings.ToLower(argv[0])
		prog = prog[strings.LastIndexAny(prog, `/\`)+1:]
		prog = strings.TrimSuffix(prog, ".exe")
		switch prog {
		case "bash", "sh", "zsh", "dash":
			if argv[1] == "-c" || argv[1] == "-lc" {
				return strings.TrimPrefix(argv[2], serverPrelude)
			}
		case "powershell", "pwsh":
			for i, a := range argv[1 : len(argv)-1] {
				if strings.EqualFold(a, "-Command") {
					return strings.Join(argv[i+2:], " ")
				}
			}
		}
	}
	return strings.Join(argv, " ")
}

// intentOf is the server's _meta.intent of a request.
func intentOf(params json.RawMessage) string {
	var p struct {
		Meta meta `json:"_meta"`
	}
	_ = json.Unmarshal(params, &p)
	return p.Meta.Intent
}
