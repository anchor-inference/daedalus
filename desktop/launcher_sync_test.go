package main

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
)

// The release's files reach the disk before they are swapped in, and each swap's renames before the
// journal moves on: a power cut after "upgraded" must not leave an empty launcher in place. Seen
// through the hook, in the order the syncs happen.
func TestAnUpgradeWritesTheLaunchersFilesToTheDiskBeforeRelyingOnThem(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	var synced []string
	launcherSyncHook = func(path string) { synced = append(synced, path) }
	defer func() { launcherSyncHook = nil }()
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	stagedLauncher, rootAfterSwap := -1, -1
	for i, path := range synced {
		switch {
		case strings.HasSuffix(path, filepath.FromSlash("new/"+launcherName())) && stagedLauncher < 0:
			stagedLauncher = i
		case path == in.root && rootAfterSwap < 0:
			rootAfterSwap = i
		}
	}
	if stagedLauncher < 0 || rootAfterSwap < 0 || stagedLauncher > rootAfterSwap {
		t.Fatalf("the new launcher was not on the disk before its swap was (staged %d, swap %d): %v", stagedLauncher, rootAfterSwap, synced)
	}
}
