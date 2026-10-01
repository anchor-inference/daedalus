//go:build windows

package main

import (
	"fmt"
	"os"
	"testing"

	"golang.org/x/sys/windows/registry"
)

// After an upgrade Apps & features names the version now installed — in this installation's own
// entry, and in no other. The two entries are made for the test under a name of its own and
// removed afterwards; nothing else in the hive is written.
func TestAppsAndFeaturesNamesTheVersionAnUpgradeInstalled(t *testing.T) {
	root := t.TempDir()
	other := t.TempDir()
	const base = `Software\Microsoft\Windows\CurrentVersion\Uninstall\`
	entry := func(name, folder string) string {
		path := base + fmt.Sprintf("daedalus-test-%d-%s", os.Getpid(), name)
		key, _, err := registry.CreateKey(registry.CURRENT_USER, path, registry.SET_VALUE)
		if err != nil {
			t.Skipf("cannot write the test's entry: %v", err)
		}
		defer key.Close()
		_ = key.SetStringValue("UninstallString", `"`+folder+`\Uninstall Daedalus.exe" /currentuser`)
		_ = key.SetStringValue("DisplayVersion", "0.13.0")
		t.Cleanup(func() { _ = registry.DeleteKey(registry.CURRENT_USER, path) })
		return path
	}
	ours, theirs := entry("ours", root), entry("theirs", other)
	noteInstalledVersion(root, "desktop-v0.14.0")
	read := func(path string) string {
		key, err := registry.OpenKey(registry.CURRENT_USER, path, registry.QUERY_VALUE)
		if err != nil {
			t.Fatal(err)
		}
		defer key.Close()
		value, _, err := key.GetStringValue("DisplayVersion")
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	if got := read(ours); got != "0.14.0" {
		t.Fatalf("this installation's entry says %s", got)
	}
	if got := read(theirs); got != "0.13.0" {
		t.Fatalf("another installation's entry was changed to %s", got)
	}
}
