// Package sidechan is what the harness adapters reach besides terminals: running a program for its
// output (exec.run), reading files under allowlisted roots (fs.*), and dialling the sockets and
// loopback ports a launch registered (net.dial).
//
// Each is fenced by a list. The token that opens the daemon can already start a terminal with any
// argv, so the exec list guards against mistakes rather than against a hostile host; the file roots
// and the deny list are a real wall, because they are what keeps the CLIs' credentials out of every
// read the host makes on an agent's behalf.
package sidechan

// DefaultExecAllow are the programs exec.run may run, by the basename of what PATH resolves: the
// CLIs the harness drives and what their installers and version checks need, and bash, through which
// a Daedalus agent working in a host folder runs its commands and reads and writes its files. bash
// opens everything the shell can do, which the token already does by starting a terminal with any
// argv; the list guards against mistakes, and a native agent on the host is not one. Harness adapters
// add to this list here; a deployment adds to it in the configuration file.
var DefaultExecAllow = []string{
	"claude", "codex", "opencode", "pi", "grok", "cursor-agent",
	"npm", "npx", "node", "git", "uname", "bash",
}

// DefaultDeny are the files no fs.* call reads, whatever root holds them: every CLI's login, and the
// keys and tokens a home directory keeps. `**` is any number of directories, `*` any part of one
// name. The list is compiled in and only ever extended (the configuration file), never over the
// socket; the daemon's own run and state directories are added at start.
var DefaultDeny = []string{
	"**/.claude/.credentials.json",
	"**/.codex/auth.json",
	"**/.grok/auth.json",
	"**/.cursor/**",
	"**/.local/share/opencode/auth.json",
	"**/.pi/agent/auth.json",
	"**/.ssh/**",
	"**/.gnupg/**",
	"**/.config/gh/hosts.yml",
	"**/.netrc",
	"**/.git-credentials",
	"**/.docker/config.json",
	// Beyond the CLIs': the credential stores a developer's home commonly holds. None of them is
	// anything an adapter reads, and each is a key to something outside this machine.
	"**/.aws/**",
	"**/.kube/**",
	"**/.config/gcloud/**",
	"**/.azure/**",
	"**/.npmrc",
	"**/.pypirc",
}
