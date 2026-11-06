package main

import (
	"context"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func guestPaths(t *testing.T) Paths {
	t.Helper()
	root := t.TempDir()
	return Paths{Local: filepath.Join(root, "l"), Bot: filepath.Join(root, "bot"), RuntimePython: filepath.Join(root, "py"), RuntimeVenv: filepath.Join(root, "venv"), RuntimeLogs: filepath.Join(root, "logs")}
}

func TestAnOldCheckoutHasNoDoorToStart(t *testing.T) {
	p := guestPaths(t)
	if guestDoorPresent(p) {
		t.Fatal("no file, no door")
	}
	n := NewNative(p, func(string, ...any) {})
	door, front, _ := n.newGuestDoor(context.Background(), nil)
	if door != nil || front != nil {
		t.Fatal("a door from before the native start would die at every start")
	}
	if got := guestHostEnv(p, []string{"A=1"}, nil, false, ""); len(got) != 1 {
		t.Fatalf("the app must hear nothing of a door that is not there: %v", got)
	}
}

func TestADoorComesWithItsFront(t *testing.T) {
	p := guestPaths(t)
	code := filepath.Join(p.Bot, "deploy", "guestd")
	if err := os.MkdirAll(code, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(code, "confine.py"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	n := NewNative(p, func(string, ...any) {})
	door, front, _ := n.newGuestDoor(context.Background(), map[string]string{"GUEST_DOOR_PORT": "9101"})
	if door == nil || front == nil {
		t.Fatal("both or neither")
	}
	info, err := os.Stat(guestTokenDir(p))
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("the token's folder is this user's alone: %v %v", info, err)
	}
	env := strings.Join(front.(*Process).Env, "\n")
	for _, want := range []string{"GUEST_HOST=127.0.0.1", "GUEST_DOOR_PORT=9101", "GUEST_TRUSTED_PROXIES=127.0.0.1"} {
		if !strings.Contains(env, want) {
			t.Fatalf("front env lacks %s:\n%s", want, env)
		}
	}
}

func TestTheDoorInheritsNothingFromTheOperatorsShell(t *testing.T) {
	p := guestPaths(t)
	t.Setenv("OPENAI_API_KEY", "a-key-in-the-operators-shell")
	for _, env := range [][]string{guestDoorEnv(p, "unix"), guestFrontEnv(p, nil)} {
		if strings.Contains(strings.Join(env, "\n"), "a-key-in-the-operators-shell") {
			t.Fatal("a stranger's conversation runs in this process")
		}
	}
}

func TestSocketsGiveWayToPortsWhereTheyCannotBeHad(t *testing.T) {
	p := guestPaths(t)
	if guestListen("windows", p) != "tcp" {
		t.Fatal("Python on Windows has no unix sockets")
	}
	p.Local = "/short"
	if guestListen("linux", p) != "unix" {
		t.Fatal("a short folder keeps the sockets")
	}
	p.Local = "/" + strings.Repeat("deep/", 30)
	if guestListen("darwin", p) != "tcp" {
		t.Fatal("past 104 bytes a socket cannot be bound on a Mac")
	}
}

func TestTheProxyAndTheDoorAreToldTheSamePlace(t *testing.T) {
	p := guestPaths(t)
	for _, listen := range []string{"unix", "tcp"} {
		door := strings.SplitN(guestModelEnv("GUEST_MODEL", listen, p), "=", 2)
		proxy := strings.SplitN(guestModelEnv("KEYPROXY_GUEST", listen, p), "=", 2)
		if door[1] != proxy[1] {
			t.Fatalf("%s: %s and %s", listen, door[1], proxy[1])
		}
	}
}

func TestANamespaceWallHoldsOnlyTheDoorsOwnFolders(t *testing.T) {
	p := guestPaths(t)
	argv := bwrapWall("/usr/bin/bwrap", p)
	joined := strings.Join(argv, " ")
	for _, want := range []string{"--unshare-net", "--clearenv", "--ro-bind " + guestTokenDir(p), "--bind " + guestStateDir(p)} {
		if !strings.Contains(joined, want) {
			t.Fatalf("no %q in %s", want, joined)
		}
	}
	if strings.Contains(joined, "--die-with-parent") {
		t.Fatal("that follows the launcher's thread, and Go retires threads")
	}
	for _, sealed := range []string{p.Bot + " ", filepath.Join(p.Local, "ptyd"), os.Getenv("HOME") + "/"} {
		if sealed != "/" && strings.Contains(joined, " "+sealed) {
			t.Fatalf("%q reached the wall", sealed)
		}
	}
	command := guestDoorCommand(guestWall{name: "namespaces", argv: argv}, p, []string{"A=1"}, []string{"python", "-m", "guestd"})
	if command[len(command)-4] != "--" || command[len(command)-3] != "python" {
		t.Fatalf("the door's own command comes last: %v", command[len(command)-5:])
	}
}

func TestASeatbeltDeniesHomeBeforeGivingBackTheDoorsFolders(t *testing.T) {
	p := guestPaths(t)
	profile := seatbeltProfile(p, "/Users/someone")
	deny := strings.Index(profile, `(deny file-read* file-write* (subpath "/Users/someone"))`)
	allow := strings.Index(profile, guestStateDir(p))
	if deny < 0 || allow < deny {
		t.Fatalf("the last matching rule wins, so the denial comes first:\n%s", profile)
	}
	if !strings.Contains(profile, `(deny network-outbound`) {
		t.Fatalf("no network denial:\n%s", profile)
	}
}

func TestPortsMeanNoWall(t *testing.T) {
	p := guestPaths(t)
	if wall := chooseGuestWall(context.Background(), "linux", "tcp", p, "python", func(string, ...any) {}); wall.name != "" {
		t.Fatal("a door with no network cannot be reached on a port")
	}
	if wall := chooseGuestWall(context.Background(), "windows", "unix", p, "python", func(string, ...any) {}); wall.name != "" {
		t.Fatal("nothing on Windows offers one")
	}
}

func TestAWallThatCannotBeProvedIsNotUsed(t *testing.T) {
	p := guestPaths(t)
	if err := proveGuestWall(context.Background(), guestWall{name: "seatbelt", argv: []string{"/nonexistent/wall"}}, p, "python"); err == nil {
		t.Fatal("a wall that does not run proves nothing")
	}
	if err := ensureGuestDirs(p); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(p.Bot, "deploy"), 0o755); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(t.TempDir(), "open")
	// A wrapper that runs and reports the network open: exactly what must not be believed.
	if err := os.WriteFile(script, []byte("#!/bin/sh\necho open\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := proveGuestWall(context.Background(), guestWall{name: "seatbelt", argv: []string{script}}, p, "python"); err == nil || !strings.Contains(err.Error(), "still be reached") {
		t.Fatalf("got %v", err)
	}
}

// TestARealWallHoldsARealDoor needs bubblewrap and the checkout's environment, so it runs only when
// pointed at them: DAEDALUS_TEST_VENV and DAEDALUS_TEST_PYTHON_HOME. It proves the wall as a start would,
// then runs the door inside it and reaches it through its socket, which no fake can stand in for.
func TestARealWallHoldsARealDoor(t *testing.T) {
	venv, home := os.Getenv("DAEDALUS_TEST_VENV"), os.Getenv("DAEDALUS_TEST_PYTHON_HOME")
	if venv == "" || home == "" {
		t.Skip("DAEDALUS_TEST_VENV and DAEDALUS_TEST_PYTHON_HOME are not set")
	}
	repo, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	p := guestPaths(t)
	p.Bot, p.RuntimeVenv, p.RuntimePython = repo, venv, home
	p.Local = filepath.Join("/tmp", "dg-"+strconv.Itoa(os.Getpid()))
	defer os.RemoveAll(p.Local)
	if err := ensureGuestDirs(p); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(guestTokenDir(p), "control.token"), []byte("host-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	python := filepath.Join(venv, "bin", "python")
	wall := chooseGuestWall(context.Background(), "linux", "unix", p, python, t.Logf)
	if wall.name != "namespaces" {
		t.Fatalf("no wall here: %+v", wall)
	}
	env := guestDoorEnv(p, "unix")
	argv := guestDoorCommand(wall, p, env, []string{python, "-m", "guestd"})
	door := &Process{Name: "door", Argv: argv, Dir: filepath.Join(repo, "deploy"), Env: env, LogPath: filepath.Join(p.Local, "door.log"), Log: t.Logf}
	ctx := context.Background()
	door.Start(ctx)
	defer door.Stop(ctx)
	socket := filepath.Join(guestRunDir(p), "control.sock")
	client := http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}}
	deadline := time.Now().Add(10 * time.Second)
	for {
		request, _ := http.NewRequest(http.MethodGet, "http://door/control/health", nil)
		request.Header.Set("Authorization", "Bearer host-token")
		answer, err := client.Do(request)
		if err == nil {
			answer.Body.Close()
			if answer.StatusCode != http.StatusOK {
				t.Fatalf("the door answered %d", answer.StatusCode)
			}
			return
		}
		if time.Now().After(deadline) {
			body, _ := os.ReadFile(filepath.Join(p.Local, "door.log"))
			t.Fatalf("the door never answered from inside its wall: %v\n%s", err, body)
		}
		time.Sleep(100 * time.Millisecond)
	}
}
