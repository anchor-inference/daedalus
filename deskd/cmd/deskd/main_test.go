package main

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ascorblack/daedalus/deskd/internal/ax"
)

func TestFlags(t *testing.T) {
	o, err := parse([]string{"--run-dir", "/r", "--state-dir", "/s"})
	if err != nil {
		t.Fatal(err)
	}
	if o.operatorTokenFile != filepath.Join("/s", "operator.token") || o.auditFile != filepath.Join("/s", "audit.jsonl") {
		t.Errorf("defaults %+v", o)
	}
	if o, _ := parse([]string{"--run-dir", "/r", "--state-dir", "/s", "--audit-file", "off"}); o.auditFile != "" {
		t.Errorf("audit off: %q", o.auditFile)
	}
	for _, bad := range [][]string{
		{"--state-dir", "/s"},
		{"--run-dir", "/r", "--state-dir", "/s", "--listen", "tcp:0.0.0.0:9"},
		{"--run-dir", "/r", "--state-dir", "/s", "--backend", "wayland"},
		{"--run-dir", "/r", "--state-dir", "/s", "--scene", "x.json"},
		{"--run-dir", "/r", "--state-dir", "/s", "--operator-token-file", "/r/operator.token"},
	} {
		if _, err := parse(bad); err == nil {
			t.Errorf("%s accepted", strings.Join(bad, " "))
		}
	}
}

// The launcher passes --ax-bin whatever it found; no value of it may keep the daemon from starting,
// since a desktop without the accessibility tree is still a desktop.
func TestNoAccessibilityToolFlagStopsTheDaemon(t *testing.T) {
	for _, v := range []string{"off", "fake", "/nowhere/agent-desktop", `C:\Program Files\Daedalus\agent-desktop.exe`, ""} {
		for _, backend := range []string{"native", "fake"} {
			if _, err := parse([]string{"--run-dir", "/r", "--state-dir", "/s", "--backend", backend, "--ax-bin", v}); err != nil {
				t.Errorf("--backend %s --ax-bin %q: %v", backend, v, err)
			}
		}
	}
	dir := t.TempDir()
	for v, reason := range map[string]string{"off": ax.ReasonOff, filepath.Join(dir, "missing"): ax.ReasonMissing, "fake": ax.ReasonUnsupported} {
		o := options{stateDir: dir, backendName: "native", axBin: v}
		if st := openAX(context.Background(), o, nil).Status(); st.Available || st.Reason != reason {
			t.Errorf("--ax-bin %q on a real desktop: %+v", v, st)
		}
	}
	if st := openAX(context.Background(), options{stateDir: dir, backendName: "fake"}, nil).Status(); st.Available ||
		st.Reason != ax.ReasonUnsupported {
		t.Errorf("the fake desktop with no fake tool: %+v", st)
	}
}
