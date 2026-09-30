package main

import (
	"context"
	"os"
	"strings"
	"testing"
)

// The copies of the data kept from before updates are beside the data folder, not in it: an
// uninstall that told the operator to delete the data folder by hand, and said nothing of them,
// left their keys on the disk.
func TestUninstallNamesTheKeptCopiesOfTheData(t *testing.T) {
	p := fixtureData(t)
	if err := os.WriteFile(p.Mode, []byte("native\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	app := NewApp(p)
	app.SetMode(ModeNative)
	if err := os.MkdirAll(fenceControlPath(p.Data), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, keep := range []bool{true, false} {
		app.lines = nil
		if err := app.uninstall(context.Background(), keep); err != nil {
			t.Fatal(err)
		}
		if said := strings.Join(app.lines, "\n"); !strings.Contains(said, fenceControlPath(p.Data)) {
			t.Errorf("keep=%v: the uninstall did not name %s:\n%s", keep, fenceControlPath(p.Data), said)
		}
	}
	if !exists(fenceControlPath(p.Data)) {
		t.Error("the kept copies were removed; like the data folder they are the operator's to delete")
	}
}
