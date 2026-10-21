package main

import (
	"path/filepath"
	"strings"
	"testing"
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
