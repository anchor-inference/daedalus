package rpc_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/browserd/internal/netwall"
	"github.com/ascorblack/daedalus/browserd/internal/relay/relaytest"
)

// The suite runs a second time over the attach backend with BROWSERD_BACKEND=attach: the daemon
// drives a fake of the Chrome extension (relaytest) that fronts the same Chromium and gives the
// daemon only what the extension gives it, per-tab debuggees and calls, with the relay's allowlist
// enforced. A test of what only a browser of the daemon's own does says so with launchOnly.

func attachRun() bool { return os.Getenv("BROWSERD_BACKEND") == config.BackendAttach }

// launchOnly skips a test in the attach run, with the reason it cannot hold there.
func launchOnly(t *testing.T, reason string) {
	t.Helper()
	if attachRun() {
		t.Skip("launch backend only: " + reason)
	}
}

// startFake starts the fake extension on its own Chromium, through the test's network wall so the
// fixture's names resolve as they do for the daemon's own browsers, and connects it to the daemon.
func startFake(t *testing.T, cfg *config.Config, wall *netwall.Browsers, args []string, dir string) *relaytest.Fake {
	t.Helper()
	proxy, err := wall.Open("relay")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { wall.Close("relay") })
	f, err := relaytest.Start(relaytest.Options{RunDir: cfg.RunDir, Dir: filepath.Join(dir, "chrome"), Proxy: proxy, Args: args})
	if err != nil {
		t.Fatalf("the fake extension: %v", err)
	}
	return f
}
