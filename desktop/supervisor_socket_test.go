package main

import (
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// A socket path longer than sun_path cannot be bound at all: macOS's limit is 104 bytes, and a long
// home folder under ~/Library/Application Support/Daedalus/State/<key> goes past it. The supervisor
// then listens on loopback TCP, as on Windows.
func TestALongSupervisorSocketPathFallsBackToLoopbackTCP(t *testing.T) {
	local := filepath.Join("/Users", strings.Repeat("someone-with-a-long-name", 3), "Library", "Application Support", "Daedalus", "State", "data-0123456789ab")
	long := Paths{Local: local, SupervisorSocket: filepath.Join(local, "supervisor.sock")}
	if !supervisorOverTCP(long, "darwin") {
		t.Fatalf("a %d-byte socket path is used as a socket", len(long.SupervisorSocket))
	}
	short := Paths{SupervisorSocket: "/home/someone/.local/state/daedalus/data-0123456789ab/supervisor.sock"}
	if supervisorOverTCP(short, "darwin") || supervisorOverTCP(short, "linux") {
		t.Fatal("a short socket path is not used")
	}
	if !supervisorOverTCP(short, "windows") {
		t.Fatal("Windows is given a socket")
	}
	if runtime.GOOS == "windows" {
		return
	}
	paths := fixtureData(t)
	paths.SupervisorSocket = long.SupervisorSocket
	env := strings.Join(supervisorEnv(paths, nil, nil), "\n")
	if !strings.Contains(env, "DAEDALUS_SUPERVISOR_TCP=127.0.0.1:") || strings.Contains(env, "DAEDALUS_SUPERVISOR_SOCKET=") {
		t.Fatalf("the supervisor's environment does not carry the TCP fallback:\n%s", env)
	}
}
