package device_test

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/device"
	"github.com/ascorblack/daedalus/cli/internal/exec"
	"github.com/ascorblack/daedalus/cli/internal/guard"
)

// TestServeAnExternalRelay is this client's half of an end-to-end run against a real server's relay,
// which the server's own test suite drives: it connects the device link to the server named by
// DAEDALUS_TEST_RELAY_SERVER (the API's http base) with DAEDALUS_TEST_RELAY_TOKEN, under the guard of
// DAEDALUS_TEST_RELAY_GUARD, and serves until the file DAEDALUS_TEST_RELAY_STOP appears or the server
// ends the link for good, writing how it ended into DAEDALUS_TEST_RELAY_STATUS.
// DAEDALUS_TEST_RELAY_CLIENT names the process, so the server's test can replace it on purpose.
// Without the variables it skips.
func TestServeAnExternalRelay(t *testing.T) {
	server := os.Getenv("DAEDALUS_TEST_RELAY_SERVER")
	if server == "" {
		t.Skip("no external relay named")
	}
	stop, status := os.Getenv("DAEDALUS_TEST_RELAY_STOP"), os.Getenv("DAEDALUS_TEST_RELAY_STATUS")
	level, err := guard.ParseLevel(os.Getenv("DAEDALUS_TEST_RELAY_GUARD"))
	if err != nil {
		t.Fatal(err)
	}
	ex, err := exec.New(nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	link := device.New(device.Options{Server: server, Token: os.Getenv("DAEDALUS_TEST_RELAY_TOKEN"),
		Client: os.Getenv("DAEDALUS_TEST_RELAY_CLIENT"), ClientVersion: "test",
		Executor: ex, Guard: guard.New(level, nil), MinBackoff: 300 * time.Millisecond, MaxBackoff: time.Second,
		OnStatus: func(s device.Status) { t.Logf("link %s %v", s.State, s.Err) }})
	ended := make(chan error, 1)
	go func() { ended <- link.Run(ctx) }()
	outcome := "stopped"
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
wait:
	for {
		select {
		case err := <-ended:
			switch {
			case errors.Is(err, device.ErrUnauthorized):
				outcome = "unauthorized"
			case errors.Is(err, device.ErrReplaced):
				outcome = "replaced"
			case err != nil:
				outcome = "error: " + err.Error()
			}
			break wait
		case <-ticker.C:
			if _, err := os.Stat(stop); err == nil {
				cancel()
				<-ended
				break wait
			}
		}
	}
	if status != "" {
		if err := os.WriteFile(status, []byte(outcome), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
