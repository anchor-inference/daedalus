package term

import (
	"os"
	"runtime"
	"sort"
	"strings"
)

// TagName is the variable every process of a terminal inherits, set to the terminal's id. It finds
// a process that left both the terminal's tree and its session, to end it and to count what it costs.
const TagName = "DAEDALUS_TERMINAL_ID"

// strippedEnv are removed from every spawned process's environment, whatever the caller asks. Each
// either belongs to the daemon (its own settings, or the ids of a terminal it runs inside when it is
// being developed from one), or tells a program that it runs inside some other terminal or
// multiplexer and so changes its behaviour, or — CLAUDE* — would make a coding CLI believe it is
// nested inside itself. UV_PROJECT_ENVIRONMENT is the container image's own virtualenv: inherited,
// it sent `uv add` in any project to that read-only virtualenv instead of the project's .venv.
var strippedEnv = []string{
	"DAEDALUS_PTYD_*", "DAEDALUS_TERMINAL_ID", "DAEDALUS_LAUNCH_*", "DAEDALUS_HOOK_*", "DAEDALUS_DIAL_DIR",
	"DAEDALUS_SI_*", "DAEDALUS_SHELL_LOGIN", "DAEDALUS_USER_ZDOTDIR",
	"TERM_PROGRAM*", "VSCODE_*", "TMUX*", "STY", "WINDOW", "KITTY_*", "ITERM_*", "WT_SESSION",
	"CLAUDE*", "UV_PROJECT_ENVIRONMENT",
}

// keptEnv are inherited although a pattern above matches them; only the caller's strip_env removes
// one. CLAUDE_CONFIG_DIR is where
// a Claude Code whose configuration is not in ~/.claude keeps its login and settings: stripped with
// the rest of CLAUDE*, that Claude starts signed out. The host cannot put it back, since it lives
// in the daemon's own environment on the operator's machine.
var keptEnv = map[string]bool{"CLAUDE_CONFIG_DIR": true}

// foldEnv says that variable names are one name whatever their case, as on Windows, where the
// inherited "Path" and a launch's "PATH" are the same variable: kept apart, the program would get
// both, and which one it read would be up to its runtime. A variable so the tests can turn it on.
var foldEnv = runtime.GOOS == "windows"

// envKey is the name a variable is known by: itself, or on Windows its upper case.
func envKey(name string) string {
	if foldEnv {
		return strings.ToUpper(name)
	}
	return name
}

// matchEnv reports whether name matches pattern: an exact name, or a prefix followed by '*'.
func matchEnv(pattern, name string) bool {
	pattern, name = envKey(pattern), envKey(name)
	if p, ok := strings.CutSuffix(pattern, "*"); ok {
		return strings.HasPrefix(name, p)
	}
	return pattern == name
}

// SelectInheritedEnv admits exact names only. An empty list passes no daemon environment through.
// Matching uses the platform's environment key rule so Windows cannot retain a differently cased
// copy of a credential that a launch meant to exclude.
func SelectInheritedEnv(inherited []string, names []string) []string {
	allowed := make(map[string]bool, len(names))
	for _, name := range names {
		allowed[envKey(name)] = true
	}
	selected := make([]string, 0, len(names))
	for _, kv := range inherited {
		name, _, ok := strings.Cut(kv, "=")
		if ok && allowed[envKey(name)] {
			selected = append(selected, kv)
		}
	}
	return selected
}

func isUTF8Locale(v string) bool {
	v = strings.ToLower(v)
	return strings.Contains(v, "utf-8") || strings.Contains(v, "utf8")
}

// BuildEnv is the environment of a spawned process: the inherited one without the stripped names
// and the caller's strip patterns, plus the terminal's own settings, plus the caller's additions on
// top (so the host can still choose, say, a different TERM for one launch).
func BuildEnv(inherited []string, strip []string, extra map[string]string, terminalID string) []string {
	// Values by envKey, and the spelling each name was first given, so a Windows "Path" stays "Path"
	// when a launch sets "PATH".
	env := map[string]string{}
	spelling := map[string]string{}
	var order []string
	set := func(k, v string) {
		key := envKey(k)
		if _, seen := env[key]; !seen {
			order = append(order, key)
			spelling[key] = k
		}
		env[key] = v
	}
	for _, kv := range inherited {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || k == "" {
			continue
		}
		dropped := false
		for _, p := range strippedEnv {
			if matchEnv(p, k) && !keptEnv[envKey(k)] {
				dropped = true
				break
			}
		}
		for _, p := range strip {
			if matchEnv(p, k) {
				dropped = true
				break
			}
		}
		if dropped {
			continue
		}
		set(k, v)
	}
	set("TERM", "xterm-256color")
	set("COLORTERM", "truecolor")
	set("CLAUDE_CODE_NO_FLICKER", "1")
	set("CLAUDE_CODE_SCROLL_SPEED", "3")
	set("DAEDALUS_TERMINAL_ID", terminalID)

	// A UTF-8 character type, without overriding a locale the user chose. The effective one is the
	// first of LC_ALL, LC_CTYPE and LANG that is set; when it is not UTF-8, the non-UTF-8 overrides
	// are removed (they would win over LANG) and LANG becomes C.UTF-8. A host terminal whose LANG
	// is ru_RU.UTF-8 keeps it.
	effective := ""
	for _, k := range []string{"LC_ALL", "LC_CTYPE", "LANG"} {
		if v := env[envKey(k)]; v != "" {
			effective = v
			break
		}
	}
	if !isUTF8Locale(effective) {
		for _, k := range []string{"LC_ALL", "LC_CTYPE"} {
			if v, ok := env[envKey(k)]; ok && !isUTF8Locale(v) {
				delete(env, envKey(k))
			}
		}
		set("LANG", "C.UTF-8")
	}

	keys := make([]string, 0, len(extra))
	for k := range extra {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		set(k, extra[k])
	}

	out := make([]string, 0, len(env))
	for _, key := range order {
		if v, ok := env[key]; ok {
			out = append(out, spelling[key]+"="+v)
		}
	}
	return out
}

// SetPWD makes PWD name dir, the directory a program is started in. Inherited, PWD would be the
// daemon's own directory, which a program that reads the variable takes for its own. And a shell
// that finds no PWD naming its directory works one out from the kernel, which gives the physical
// path: on macOS a terminal opened in /var/folders/... or /tmp then says it is in /private/var/...
// or /private/tmp. A shell keeps a PWD only after checking it names the directory it is in, so the
// path asked for is what it shows. Windows has no such variable.
func SetPWD(extra map[string]string, dir string) {
	if runtime.GOOS != "windows" {
		extra["PWD"] = dir
	}
}

// LookPath resolves a program name against the PATH of the environment it will run in, not the
// daemon's own: the two differ as soon as the host passes a PATH for a launch.
func LookPath(name string, env []string, dir string) (string, bool) {
	if runtime.GOOS == "windows" {
		return lookPathWindows(name, env, dir, isFile)
	}
	return lookPathUnix(name, env, dir)
}

func lookPathUnix(name string, env []string, dir string) (string, bool) {
	if strings.Contains(name, "/") {
		p := name
		if !strings.HasPrefix(p, "/") {
			p = dir + "/" + p
		}
		return p, isExecutable(p)
	}
	path := ""
	for _, kv := range env {
		if v, ok := strings.CutPrefix(kv, "PATH="); ok {
			path = v
		}
	}
	if path == "" {
		path = "/usr/local/bin:/usr/bin:/bin"
	}
	for _, d := range strings.Split(path, ":") {
		if d == "" {
			d = "."
		}
		p := d + "/" + name
		if !strings.HasPrefix(p, "/") {
			p = dir + "/" + p
		}
		if isExecutable(p) {
			return p, true
		}
	}
	return "", false
}

// windowsExts is what PATHEXT holds when an environment does not say: the extensions a name may
// leave off and still be run.
const windowsExts = ".COM;.EXE;.BAT;.CMD"

// lookPathWindows is LookPath by Windows rules, with the file check passed in so any platform can
// test it: names are case-insensitive, PATH is split on ';', a name without an extension is tried
// with each of PATHEXT (a CLI installed by npm is a .cmd), and a name with a directory in it is
// taken relative to dir. The current directory is not searched first, as cmd.exe would: a program
// named like a system one in a project folder would run instead of it.
func lookPathWindows(name string, env []string, dir string, exists func(string) bool) (string, bool) {
	var path, pathext string
	for _, kv := range env {
		k, v, _ := strings.Cut(kv, "=")
		switch strings.ToUpper(k) {
		case "PATH":
			path = v
		case "PATHEXT":
			pathext = v
		}
	}
	if pathext == "" {
		pathext = windowsExts
	}
	var exts []string
	for _, e := range strings.Split(strings.ToLower(pathext), ";") {
		if e = strings.TrimSpace(e); e != "" {
			if !strings.HasPrefix(e, ".") {
				e = "." + e
			}
			exts = append(exts, e)
		}
	}
	try := func(p string) (string, bool) {
		ext := strings.ToLower(windowsExt(p))
		for _, e := range exts {
			if ext == e && exists(p) {
				return p, true
			}
		}
		for _, e := range exts {
			if exists(p + e) {
				return p + e, true
			}
		}
		return "", false
	}
	if strings.ContainsAny(name, `/\:`) {
		p := name
		if !windowsAbs(p) {
			p = strings.TrimRight(dir, `/\`) + `\` + p
		}
		return try(p)
	}
	for _, d := range strings.Split(path, ";") {
		d = strings.Trim(strings.TrimSpace(d), `"`)
		if d == "" {
			continue
		}
		if p, ok := try(strings.TrimRight(d, `/\`) + `\` + name); ok {
			return p, true
		}
	}
	return "", false
}

// windowsExt is the extension of the last element of a Windows path, dot included.
func windowsExt(p string) string {
	base := p[strings.LastIndexAny(p, `/\`)+1:]
	if i := strings.LastIndexByte(base, '.'); i >= 0 {
		return base[i:]
	}
	return ""
}

// windowsAbs reports a path that names its drive and its root ("C:\x") or a share ("\\host\x").
func windowsAbs(p string) bool {
	if strings.HasPrefix(p, `\\`) || strings.HasPrefix(p, "//") {
		return true
	}
	return len(p) >= 3 && p[1] == ':' && (p[2] == '\\' || p[2] == '/')
}

// isFile is the check of a Windows program: a file that exists. Windows keeps no execute bit.
func isFile(p string) bool {
	st, err := os.Stat(p)
	return err == nil && !st.IsDir()
}
