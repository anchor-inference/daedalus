//go:build linux

package rpc

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"testing"
)

func TestDiskPreflightSelectedWorkspace(t *testing.T) {
	workspace := t.TempDir()
	observed := diskPreflight(workspace)
	if !observed.Available || !observed.WorkspaceExists || observed.FreeBytes <= 0 {
		t.Fatalf("existing workspace should have a measured volume: %+v", observed)
	}
	if observed.DeviceID == "" || observed.MountID == "" || observed.ObservedAt == "" {
		t.Fatalf("volume identity and observation time are required: %+v", observed)
	}
	sum := sha256.Sum256([]byte(workspace))
	if observed.PathDigest != hex.EncodeToString(sum[:]) {
		t.Fatalf("observation is not bound to the requested path: %+v", observed)
	}

	future := filepath.Join(workspace, "future", "worker")
	beforeCreation := diskPreflight(future)
	if !beforeCreation.Available || beforeCreation.WorkspaceExists {
		t.Fatalf("future workspace should use its existing ancestor: %+v", beforeCreation)
	}
	if beforeCreation.DeviceID != observed.DeviceID || beforeCreation.MountID != observed.MountID {
		t.Fatalf("future workspace changed the selected volume: %+v", beforeCreation)
	}
	if diskPreflight("relative/workspace").Available {
		t.Fatal("relative workspace cannot identify a selected volume")
	}
}
