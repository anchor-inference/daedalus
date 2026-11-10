package main

// The guest door in native mode: where other people's agents arrive on an installation that runs on the
// operator's own machine (deploy/guestd). In compose it is two containers, and the one that holds a
// stranger's conversation has no network, no state of the agent's and a user of its own. Natively there
// is one user and the launcher starts the same two programs under the runtime's python, beside the agent
// and not under it, like the key proxy: the door must outlive an applied change of the agent.
//
// What is lost, and what is kept. The door runs as the operator, so the wall a container gave it has to
// be built again here, and how much of it can be depends on the system (guestWall). On Linux with a
// working bubblewrap it comes back nearly whole: no network at all, and a root holding the door's own
// folders. The front parses strangers' bytes first and needs the loopback network, so it gets the same
// root with the network left in. On a Mac a seatbelt that starts from denying everything is tried first,
// and if it does not hold one that allows everything but IP and the home folder, which is hygiene
// rather than a wall, and the Health screen says which. On Windows nothing here is a wall: the door is an
// ordinary program of the operator's, kept from dialling out and from starting programs only by its own
// audit hook (deploy/guestd/confine.py), and what holds there is that there is nothing in it worth
// taking and no code in it that reaches for anything. SECURITY.md says the same to the operator.
//
// The checkouts move apart from this binary (UPDATES.md), so a launcher can meet a checkout whose door
// is older than the native start, or has none. guestDoorPresent is what decides: no door then, and the
// app says the door is off, as it did before this existed.

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

func guestDir(p Paths, name string) string { return filepath.Join(p.Local, "guest", name) }

func guestStateDir(p Paths) string { return guestDir(p, "state") }
func guestRunDir(p Paths) string   { return guestDir(p, "run") }
func guestFrontDir(p Paths) string { return guestDir(p, "front") }
func guestTokenDir(p Paths) string { return guestDir(p, "token") }
func guestModelDir(p Paths) string { return guestDir(p, "model") }

// guestDoorPresent is whether this checkout's door can be started from here. It names the file that
// came with the native start, not the package: a door from before it cannot listen on a port, and on
// Windows would die at every start.
func guestDoorPresent(p Paths) bool {
	return exists(filepath.Join(p.Bot, "deploy", "guestd", "confine.py"))
}

// guestListen is how the door's three sockets are reached: unix sockets, or loopback ports on a system
// whose Python has none and where the folder is too deep for one (104 bytes on a Mac, and an operator
// can drop the app anywhere).
func guestListen(goos string, p Paths) string {
	longest := filepath.Join(guestFrontDir(p), "public.endpoint")
	if model := filepath.Join(guestModelDir(p), "guest.endpoint"); len(model) > len(longest) {
		longest = model
	}
	if goos == "windows" || len(longest) > 100 {
		return "tcp"
	}
	return "unix"
}

// guestModelEnv is what names the key proxy's guest listener, to the proxy and to the door alike: the
// socket, or the file the proxy writes its port to.
func guestModelEnv(prefix string, listen string, p Paths) string {
	if listen == "tcp" {
		return prefix + "_ENDPOINT=" + filepath.Join(guestModelDir(p), "guest.endpoint")
	}
	return prefix + "_SOCKET=" + filepath.Join(guestModelDir(p), "guest.sock")
}

// guestPlainEnv is the whole environment of the door and its front. Built from nothing rather than from
// the launcher's own: the operator's shell may hold a key, and a stranger's conversation runs in here.
func guestPlainEnv(p Paths) []string {
	env := []string{"PYTHONUTF8=1", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=" + filepath.Join(p.Bot, "deploy"), "HOME=" + guestStateDir(p), "GUEST_CONFINE=1"}
	for _, key := range []string{"LANG", "SYSTEMROOT", "TEMP", "TMP"} {
		if value := os.Getenv(key); value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}

func guestDoorEnv(p Paths, listen string) []string {
	return append(guestPlainEnv(p),
		"GUEST_STATE_DIR="+guestStateDir(p),
		"GUEST_RUN_DIR="+guestRunDir(p),
		"GUEST_FRONT_DIR="+guestFrontDir(p),
		"GUEST_TOKEN_DIR="+guestTokenDir(p),
		"GUEST_LISTEN="+listen,
		guestModelEnv("GUEST_MODEL", listen, p),
	)
}

// guestFrontEnv stands the front on loopback only. What reaches it from outside comes through the
// router of a tunnel, which is the one peer allowed to say who a visitor is, and which says it itself.
func guestFrontEnv(p Paths, settings map[string]string) []string {
	return append(guestPlainEnv(p),
		"GUEST_HOST=127.0.0.1",
		"GUEST_DOOR_PORT="+parsePort(settings["GUEST_DOOR_PORT"], portDefaults["GUEST_DOOR_PORT"]),
		"GUEST_FRONT_DIR="+guestFrontDir(p),
		"GUEST_TRUSTED_PROXIES=127.0.0.1",
		"GUEST_LISTEN="+guestListen(runtime.GOOS, p),
	)
}

// guestUpstreams is which models a stand-in may use, the same setting compose passes both ways.
func guestUpstreams(settings map[string]string) string {
	if named := strings.TrimSpace(settings["KEYPROXY_GUEST_UPSTREAMS"]); named != "" {
		return named
	}
	return "*"
}

// guestWall is what the door runs inside on this system: the command in front of the door's own, and
// its name for the log and the Health screen. "" means the audit hook alone.
type guestWall struct {
	name string
	argv []string
	// front is the front's own wall where it needs another; empty leaves it to run as it is.
	front []string
}

// bwrapLocations are the fixed places bubblewrap is looked for, as the agent's sandbox looks for it: a
// PATH can hold a folder an agent wrote to, and a planted bwrap would only pretend to be a wall.
var bwrapLocations = []string{"/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap", "/usr/sbin/bwrap", "/run/current-system/sw/bin/bwrap"}

func guestDoorCode(p Paths) string { return filepath.Join(p.Bot, "deploy", "guestd") }

// bwrapWall is an empty root with the system's libraries, the interpreter, the door's code and its own
// folders, and no network at all. Without --die-with-parent: that follows the launcher's thread and not
// its process, and the Go runtime retires threads; the door's end is the launcher's business anyway.
//
// Without --new-session too, on purpose: inside the wall it would move the door out of the process group
// the launcher signals, so a stop would end bwrap and leave the door running. What that flag is for, keeping
// a program from a terminal it could type into, comes from the launcher starting these in a session of
// their own (Process.NewSession), where the group and the signal still reach them.
func bwrapWall(binary string, p Paths) []string {
	argv := append(bwrapBase(binary), "--unshare-net")
	for _, dir := range []string{p.RuntimePython, p.RuntimeVenv, guestDoorCode(p), guestTokenDir(p), guestModelDir(p)} {
		argv = append(argv, "--ro-bind", dir, dir)
	}
	for _, dir := range []string{guestStateDir(p), guestRunDir(p), guestFrontDir(p)} {
		argv = append(argv, "--bind", dir, dir)
	}
	return argv
}

// bwrapFrontWall is the root the front gets. It holds no state and no token, and the network stays
// because a port on loopback is the front's whole job.
func bwrapFrontWall(binary string, p Paths) []string {
	argv := bwrapBase(binary)
	for _, dir := range []string{p.RuntimePython, p.RuntimeVenv, guestDoorCode(p), guestFrontDir(p)} {
		argv = append(argv, "--ro-bind", dir, dir)
	}
	return argv
}

func bwrapBase(binary string) []string {
	argv := []string{binary, "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--cap-drop", "ALL", "--clearenv",
		"--tmpfs", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"}
	for _, dir := range []string{"/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc/ld.so.cache", "/etc/localtime"} {
		argv = append(argv, "--ro-bind-try", dir, dir)
	}
	return argv
}

func sbpl(path string) string { return strconv.Quote(path) }

func seatbeltDirs(b *strings.Builder, p Paths) {
	for _, dir := range []string{p.RuntimePython, p.RuntimeVenv, guestDoorCode(p), guestTokenDir(p), guestModelDir(p)} {
		fmt.Fprintf(b, "(allow file-read* (subpath %s))\n", sbpl(dir))
	}
	for _, dir := range []string{guestStateDir(p), guestRunDir(p), guestFrontDir(p)} {
		fmt.Fprintf(b, "(allow file-read* file-write* (subpath %s))\n", sbpl(dir))
	}
}

// strictSeatbelt starts from nothing: no program to start, no mach service but what the system's own base
// profile opens, no socket but the door's own and the proxy's. Nobody has run it on a Mac from here, which is
// why it is one candidate and proves itself by the probe rather than being assumed.
func strictSeatbelt(p Paths, python string) string {
	var b strings.Builder
	b.WriteString("(version 1)\n(deny default)\n(import \"system.sb\")\n")
	b.WriteString("(allow process-fork)\n(allow signal (target self))\n(allow sysctl-read)\n(allow file-read-metadata)\n")
	fmt.Fprintf(&b, "(allow process-exec (literal %s))\n", sbpl(python))
	for _, dir := range []string{"/usr/lib", "/usr/share", "/System", "/Library/Preferences", "/dev", "/private/var/db/timezone", "/etc", "/private/etc", "/var"} {
		fmt.Fprintf(&b, "(allow file-read* (subpath %s))\n", sbpl(dir))
	}
	seatbeltDirs(&b, p)
	for _, dir := range []string{guestRunDir(p), guestFrontDir(p), guestModelDir(p)} {
		fmt.Fprintf(&b, "(allow network* (local unix-socket (subpath %s)))\n(allow network* (remote unix-socket (subpath %s)))\n", sbpl(dir), sbpl(dir))
	}
	return b.String()
}

// hygieneSeatbelt allows everything and takes away IP and the home folder. A door taken over here keeps
// starting programs, mach services, and unix sockets and writes outside the home folder, so this is
// hygiene and not a wall, and the Health screen calls it that. The last matching rule wins in this language,
// which is why the order reads backwards.
func hygieneSeatbelt(p Paths, home string) string {
	var b strings.Builder
	b.WriteString("(version 1)\n(allow default)\n")
	b.WriteString("(deny network-outbound (remote ip \"*:*\"))\n(deny network-inbound (local ip \"*:*\"))\n(deny network-bind (local ip \"*:*\"))\n")
	if home != "" {
		fmt.Fprintf(&b, "(deny file-read* file-write* (subpath %s))\n", sbpl(home))
	}
	seatbeltDirs(&b, p)
	return b.String()
}

// guestProbe is run inside a candidate wall. It says "walled" only if the door could live there and the
// network could not be reached from it: its imports load, a socket can be made in its folder, and the
// loopback port the launcher holds open does not answer. A wall that cannot be checked is not used,
// because on a Mac or a distribution nobody here has run, the check is all there is.
const guestProbe = `
import socket, sys, os
import asyncio, sqlite3
import guestd.confine, uvicorn, starlette, httpx
connection = sqlite3.connect(os.path.join(sys.argv[4], "probe.sqlite"))
connection.execute("CREATE TABLE IF NOT EXISTS t (x)")
connection.close()
os.unlink(os.path.join(sys.argv[4], "probe.sqlite"))
asyncio.run(asyncio.sleep(0))
path = os.path.join(sys.argv[1], "probe.sock")
if sys.argv[3] == "unix":
    a = socket.socket(socket.AF_UNIX)
    a.bind(path)
    a.listen(1)
    a.close()
    os.unlink(path)
b = socket.socket()
b.settimeout(2)
try:
    b.connect(("127.0.0.1", int(sys.argv[2])))
except OSError:
    print("walled")
else:
    print("open")
`

// chooseGuestWall tries what this system offers and keeps what proves itself. A wall needs unix sockets,
// because a door with no network cannot be reached on a port.
func chooseGuestWall(ctx context.Context, goos, listen string, p Paths, python string, log func(string, ...any)) guestWall {
	if listen != "unix" {
		return guestWall{}
	}
	var candidates []guestWall
	switch goos {
	case "linux":
		for _, binary := range bwrapLocations {
			if exists(binary) {
				candidates = append(candidates, guestWall{name: "namespaces", argv: bwrapWall(binary, p), front: bwrapFrontWall(binary, p)})
				break
			}
		}
	case "darwin":
		if exists("/usr/bin/sandbox-exec") {
			home, _ := os.UserHomeDir()
			candidates = append(candidates,
				guestWall{name: "seatbelt", argv: []string{"/usr/bin/sandbox-exec", "-p", strictSeatbelt(p, python)}},
				guestWall{name: "seatbelt-hygiene", argv: []string{"/usr/bin/sandbox-exec", "-p", hygieneSeatbelt(p, home)}},
			)
		}
	}
	for _, candidate := range candidates {
		err := proveGuestWall(ctx, candidate, p, python)
		if err == nil {
			return candidate
		}
		log("the guest door's wall: %s would not hold (%v)", candidate.name, err)
	}
	return guestWall{}
}

func proveGuestWall(ctx context.Context, wall guestWall, p Paths, python string) error {
	held, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer held.Close()
	port := strconv.Itoa(held.Addr().(*net.TCPAddr).Port)
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	argv := guestDoorCommand(wall.name, wall.argv, p, guestPlainEnv(p), []string{python, "-c", guestProbe, guestRunDir(p), port, "unix", guestStateDir(p)})
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = guestPlainEnv(p)
	cmd.Dir = filepath.Join(p.Bot, "deploy")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	if strings.TrimSpace(string(out)) != "walled" {
		return fmt.Errorf("the network could still be reached from inside it")
	}
	return nil
}

// guestDoorCommand puts the door's command behind its wall. A namespace wall clears the environment, so
// the door's is handed over in its own words; a seatbelt keeps the one it was started with.
func guestDoorCommand(name string, wall []string, p Paths, env, command []string) []string {
	argv := append([]string(nil), wall...)
	if name == "namespaces" {
		argv = append(argv, "--chdir", filepath.Join(p.Bot, "deploy"))
		for _, kv := range env {
			key, value, _ := strings.Cut(kv, "=")
			argv = append(argv, "--setenv", key, value)
		}
		argv = append(argv, "--")
	}
	return append(argv, command...)
}

// guestFrontCommand walls the front where the door's wall has a form for it. A seatbelt's would have to allow
// the listening socket and the network it just denied, so on a Mac the front stands outside: it holds
// nothing, and its own audit hook is what is left there.
func guestFrontCommand(wall guestWall, p Paths, env []string, python string) []string {
	command := []string{python, "-m", "guestd.front"}
	if len(wall.front) == 0 {
		return command
	}
	return guestDoorCommand(wall.name, wall.front, p, env, command)
}

// ensureGuestDirs makes the door's folders, private to this user. The model folder is the key proxy's
// to write and the token folder the agent's; the door reads both. The mode means nothing on Windows,
// where restrictToUser is what keeps the token from another account on the same machine.
func ensureGuestDirs(p Paths) error {
	for _, dir := range []string{guestStateDir(p), guestRunDir(p), guestFrontDir(p), guestTokenDir(p), guestModelDir(p)} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return err
		}
		if err := restrictToUser(dir); err != nil {
			return fmt.Errorf("making %s private: %w", dir, err)
		}
	}
	return nil
}

// newGuestDoor is the door and its front, or nothing when this checkout carries no door to start. Its
// failure stops nothing: the app says the door is off, and the rest of the installation does not care.
func (n *Native) newGuestDoor(ctx context.Context, settings map[string]string) (door, front child, wall guestWall) {
	if !guestDoorPresent(n.paths) {
		n.log("this checkout has no guest door to start natively; agent links stay off until it is updated")
		return nil, nil, guestWall{}
	}
	if err := ensureGuestDirs(n.paths); err != nil {
		n.log("the guest door's folders: %v", err)
		return nil, nil, guestWall{}
	}
	python := n.venvPython()
	listen := guestListen(runtime.GOOS, n.paths)
	wall = chooseGuestWall(ctx, runtime.GOOS, listen, n.paths, python, n.log)
	env := guestDoorEnv(n.paths, listen)
	code := filepath.Join(n.paths.Bot, "deploy")
	door = &Process{
		Name:       "guest door",
		Argv:       guestDoorCommand(wall.name, wall.argv, n.paths, env, []string{python, "-m", "guestd"}),
		Dir:        code,
		Env:        env,
		LogPath:    filepath.Join(n.paths.RuntimeLogs, "guestd.log"),
		Log:        n.log,
		PidFile:    filepath.Join(pidsDir(n.paths), "guestd.json"),
		NewSession: true,
	}
	frontEnv := guestFrontEnv(n.paths, settings)
	front = &Process{
		Name:       "guest front",
		Argv:       guestFrontCommand(wall, n.paths, frontEnv, python),
		Dir:        code,
		Env:        frontEnv,
		LogPath:    filepath.Join(n.paths.RuntimeLogs, "guest-front.log"),
		Log:        n.log,
		PidFile:    filepath.Join(pidsDir(n.paths), "guest-front.json"),
		NewSession: true,
	}
	return door, front, wall
}

// guestProxyEnv opens the key proxy's guest listener. Whether or not this checkout has a door: a proxy
// that listens for nobody costs one idle socket, and an old proxy ignores the names.
func guestProxyEnv(p Paths, env []string, settings map[string]string) []string {
	return append(env, guestModelEnv("KEYPROXY_GUEST", guestListen(runtime.GOOS, p), p), "KEYPROXY_GUEST_UPSTREAMS="+guestUpstreams(settings))
}

// guestHostEnv tells the app about the door, and only when there is one: unset, it says the door is off.
// The door runs as this same user, so there is no other uid to check its socket's peer against.
func guestHostEnv(p Paths, env []string, settings map[string]string, present bool, wall string) []string {
	if !present {
		return env
	}
	return append(env,
		"GUEST_RUN_DIR="+guestRunDir(p),
		"GUEST_TOKEN_DIR="+guestTokenDir(p),
		"GUEST_DOOR_UID=-1",
		"GUEST_LISTEN="+guestListen(runtime.GOOS, p),
		"GUEST_MODEL_UPSTREAMS="+guestUpstreams(settings),
		"GUEST_DOOR_WALL="+wall,
	)
}
