package main

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// fixtureData makes a small installation's data folder: the kinds of thing a real one holds.
func fixtureData(t *testing.T) Paths {
	t.Helper()
	p, err := NewPaths(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		".env":                          "DAEDALUS_PORT=18000\n",
		"mode":                          "native\n",
		"daedalus-secrets/keyproxy.env": "FIXTURE_KEY=not-a-real-key\n",
		"state/daedalus.sqlite":         "schema v1\n",
		"workspaces/notes/today.md":     "a note\n",
		"daedalus/app.py":               "print('v1')\n",
		"daedalus/.git/HEAD":            "ref: refs/heads/main\n",
		"runtime/venv/big":              "downloaded again, never backed up",
		"launcher.json":                 "{}",
	}
	for name, body := range files {
		full := filepath.Join(p.Data, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(p.Data, "workspaces", "empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		if err := os.Symlink("today.md", filepath.Join(p.Data, "workspaces", "notes", "latest.md")); err != nil {
			t.Fatal(err)
		}
	}
	return p
}

func read(t *testing.T, name string) string {
	t.Helper()
	body, err := os.ReadFile(name)
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

func TestABackupRestoresTheFolderAsItWas(t *testing.T) {
	p := fixtureData(t)
	dir, manifest, err := CreateBackup(p, "desktop-v0.12.0", "native", time.Unix(1_800_000_000, 0), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyBackup(dir); err != nil {
		t.Fatalf("a fresh backup does not verify: %v", err)
	}
	for _, entry := range manifest.Entries {
		if !backedUp(entry.Path) {
			t.Errorf("%s was backed up", entry.Path)
		}
	}
	info, err := os.Stat(dir)
	if err != nil || info.Mode().Perm() != 0o700 && runtime.GOOS != "windows" {
		t.Fatalf("the backup folder is %v, want 0700", info.Mode())
	}

	// What an upgrade's migration does, and what a session did on top.
	os.WriteFile(filepath.Join(p.State, "daedalus.sqlite"), []byte("schema v2\n"), 0o640)
	os.WriteFile(filepath.Join(p.Data, "state", "new-table"), []byte("x"), 0o640)
	os.WriteFile(filepath.Join(p.Data, "daedalus", "app.py"), []byte("print('v2')\n"), 0o640)

	aside := filepath.Join(p.Data, "upgrade", "replaced")
	if err := RestoreBackup(p, dir, aside); err != nil {
		t.Fatal(err)
	}
	if got := read(t, filepath.Join(p.State, "daedalus.sqlite")); got != "schema v1\n" {
		t.Fatalf("the database is %q after the restore", got)
	}
	if exists(filepath.Join(p.State, "new-table")) {
		t.Fatal("a file the migration made survived the restore")
	}
	if got := read(t, filepath.Join(p.Data, "daedalus", "app.py")); got != "print('v1')\n" {
		t.Fatalf("the checkout is %q after the restore", got)
	}
	// The runtime (here a data folder from before it moved out) and the backups are left where
	// they are; what was replaced is kept aside.
	if got := read(t, filepath.Join(p.LegacyRuntime, "venv", "big")); got == "" {
		t.Fatal("the runtime was touched")
	}
	if got := read(t, filepath.Join(aside, "state", "daedalus.sqlite")); got != "schema v2\n" {
		t.Fatalf("the replaced database was not kept aside: %q", got)
	}
	if !exists(dir) {
		t.Fatal("the restore consumed its backup")
	}
}

func TestATamperedBackupIsNotRestored(t *testing.T) {
	p := fixtureData(t)
	dir, _, err := CreateBackup(p, "desktop-v0.12.0", "native", time.Now(), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	archive := filepath.Join(dir, backupArchive)
	body := []byte(read(t, archive))
	body[len(body)/2] ^= 0xff
	if err := os.WriteFile(archive, body, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyBackup(dir); err == nil {
		t.Fatal("a changed archive verified")
	}
	os.WriteFile(filepath.Join(p.State, "daedalus.sqlite"), []byte("schema v2\n"), 0o640)
	if err := RestoreBackup(p, dir, filepath.Join(p.Data, "upgrade", "aside")); err == nil {
		t.Fatal("a changed archive was restored")
	}
	if got := read(t, filepath.Join(p.State, "daedalus.sqlite")); got != "schema v2\n" {
		t.Fatal("a refused restore changed the folder")
	}
}

// craftBackup writes a backup whose archive holds one entry named name, with a manifest that matches
// it — the shape an attacker who could write the backup folder would produce.
func craftBackup(t *testing.T, name string, kind byte, link string) string {
	t.Helper()
	dir := t.TempDir()
	file, _ := os.Create(filepath.Join(dir, backupArchive))
	hash := sha256.New()
	zipped := gzip.NewWriter(file)
	writer := tar.NewWriter(zipped)
	writer.WriteHeader(&tar.Header{Name: name, Typeflag: kind, Linkname: link, Mode: 0o644})
	writer.Close()
	zipped.Close()
	file.Close()
	sum, _, _ := sha256File(filepath.Join(dir, backupArchive))
	_ = hash
	manifest := &Manifest{Version: manifestVersion, ArchiveSHA256: sum, Entries: []BackupEntry{{Path: name, Kind: "file", SHA256: hex.EncodeToString(sha256.New().Sum(nil))}}}
	if err := writeManifest(dir, manifest); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestABackupCannotWriteOutsideTheFolder(t *testing.T) {
	for _, name := range []string{"../escape", "/etc/escape", "state/../../escape"} {
		dir := craftBackup(t, name, tar.TypeReg, "")
		if _, err := VerifyBackup(dir); err == nil {
			t.Errorf("%s: verified", name)
		}
	}
}

func TestABackupDoesNotWriteThroughALink(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("symlinks")
	}
	outside := t.TempDir()
	root := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "state")); err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	file, _ := os.Create(filepath.Join(dir, backupArchive))
	zipped := gzip.NewWriter(file)
	writer := tar.NewWriter(zipped)
	writer.WriteHeader(&tar.Header{Name: "state/owned", Typeflag: tar.TypeReg, Mode: 0o644, Size: 1})
	writer.Write([]byte("x"))
	writer.Close()
	zipped.Close()
	file.Close()
	if err := extractBackup(filepath.Join(dir, backupArchive), root); err == nil {
		t.Fatal("a file was unpacked through a link")
	}
	if exists(filepath.Join(outside, "owned")) {
		t.Fatal("a file landed outside the folder")
	}
}

func TestOnlyTheNewestBackupsAreKept(t *testing.T) {
	p := fixtureData(t)
	base := time.Unix(1_800_000_000, 0)
	var dirs []string
	for i := 0; i < 5; i++ {
		dir, _, err := CreateBackup(p, "desktop-v0.12.0", "native", base.Add(time.Duration(i)*time.Hour), "", nil)
		if err != nil {
			t.Fatal(err)
		}
		dirs = append(dirs, dir)
	}
	os.Mkdir(filepath.Join(backupsDir(p), "19990101T000000Z-x.partial"), 0o700)
	if err := PruneBackups(p, 3); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(backupsDir(p))
	if len(entries) != 3 || exists(dirs[0]) || exists(dirs[1]) || !exists(dirs[4]) {
		t.Fatalf("kept %d backups", len(entries))
	}
}

func TestTwoBackupsInOneSecondAreTwoBackups(t *testing.T) {
	p := fixtureData(t)
	at := time.Unix(1_800_000_000, 0)
	first, _, err := CreateBackup(p, "desktop-v0.12.0", "native", at, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	second, _, err := CreateBackup(p, "desktop-v0.12.0", "native", at, "", nil)
	if err != nil || second == first {
		t.Fatalf("second backup: %q, %v", second, err)
	}
	if _, err := VerifyBackup(first); err != nil {
		t.Fatal(err)
	}
}

// workspaceData is a data folder with the work in it: a session workspace with a deep tree, the
// agent's worktrees, a workspace's snapshot store inside state/, and a link to a project folder
// outside the data folder altogether.
func workspaceData(t *testing.T) (Paths, string) {
	t.Helper()
	p := fixtureData(t)
	outside := t.TempDir()
	files := map[string]string{
		"workspaces/s1/node_modules/a/b/c/index.js": "module.exports = 1\n",
		"workspaces/s1/main.py":                     "print('work')\n",
		"worktrees/selfdev/README.md":               "a worktree\n",
		"state/checkpoints/s1/HEAD":                 "ref: refs/heads/main\n",
		"state/models/stt/model.bin":                "weights",
	}
	for name, body := range files {
		full := filepath.Join(p.Data, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(outside, "project.txt"), []byte("the operator's project\n"), 0o640); err != nil {
		t.Fatal(err)
	}
	return p, outside
}

func TestABackupLeavesTheWorkspacesOutAndTheRestoreLeavesThemAlone(t *testing.T) {
	p, outside := workspaceData(t)
	if runtime.GOOS != "windows" {
		// An attached project, as a link from inside a workspace and from inside state/: neither may
		// bring the project into the backup.
		os.Symlink(outside, filepath.Join(p.Data, "workspaces", "s1", "attached"))
		os.Symlink(outside, filepath.Join(p.State, "project-link"))
	}
	// Large enough that the room check would show it, had it been counted.
	if err := os.WriteFile(filepath.Join(p.Data, "workspaces", "s1", "big.bin"), make([]byte, 1<<20), 0o640); err != nil {
		t.Fatal(err)
	}
	dir, manifest, err := CreateBackup(p, "desktop-v0.12.0", "native", time.Unix(1_800_000_000, 0), "", nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range manifest.Entries {
		for _, skipped := range []string{"workspaces", "worktrees", "state/checkpoints", "state/models"} {
			if entry.Path == skipped || strings.HasPrefix(entry.Path, skipped+"/") {
				t.Errorf("%s was backed up", entry.Path)
			}
		}
		if strings.Contains(entry.Path, "project.txt") {
			t.Errorf("the backup followed a link out of the folder: %s", entry.Path)
		}
	}
	if runtime.GOOS != "windows" {
		found := false
		for _, entry := range manifest.Entries {
			if entry.Path == "state/project-link" {
				found = entry.Kind == "symlink" && entry.Target == filepath.ToSlash(outside)
			}
		}
		if !found {
			t.Fatal("the link in state/ was not kept as a link")
		}
	}
	if need, err := backupRoom(p, "", nil); err != nil || need > 64<<20+1<<16 {
		t.Fatalf("the room a backup needs counts the workspaces: %d, %v", need, err)
	}

	// After the backup the session goes on working, a checkpoint is taken, and the update migrates.
	os.WriteFile(filepath.Join(p.Data, "workspaces", "s1", "main.py"), []byte("print('later')\n"), 0o640)
	os.WriteFile(filepath.Join(p.Data, "workspaces", "s1", "new.py"), []byte("new\n"), 0o640)
	os.WriteFile(filepath.Join(p.Data, "state", "checkpoints", "s1", "HEAD"), []byte("later\n"), 0o640)
	os.WriteFile(filepath.Join(p.State, "daedalus.sqlite"), []byte("schema v2\n"), 0o640)

	aside := filepath.Join(p.Data, "upgrade", "replaced")
	if err := RestoreBackup(p, dir, aside); err != nil {
		t.Fatal(err)
	}
	if got := read(t, filepath.Join(p.State, "daedalus.sqlite")); got != "schema v1\n" {
		t.Fatalf("the database is %q after the restore", got)
	}
	for name, want := range map[string]string{
		"workspaces/s1/main.py":                     "print('later')\n",
		"workspaces/s1/new.py":                      "new\n",
		"workspaces/s1/node_modules/a/b/c/index.js": "module.exports = 1\n",
		"worktrees/selfdev/README.md":               "a worktree\n",
		"state/checkpoints/s1/HEAD":                 "later\n",
		"state/models/stt/model.bin":                "weights",
	} {
		if got := read(t, filepath.Join(p.Data, filepath.FromSlash(name))); got != want {
			t.Errorf("%s is %q after the restore, want %q", name, got, want)
		}
	}
	if exists(filepath.Join(aside, "workspaces")) || exists(filepath.Join(aside, "state", "checkpoints")) {
		t.Fatal("work the backup does not hold was moved aside")
	}
	if runtime.GOOS != "windows" {
		if link, err := os.Readlink(filepath.Join(p.Data, "workspaces", "s1", "attached")); err != nil || link != outside {
			t.Fatalf("the attached project's link is %q, %v", link, err)
		}
	}
	if got := read(t, filepath.Join(outside, "project.txt")); got != "the operator's project\n" {
		t.Fatalf("the project outside the folder is %q", got)
	}
}

// An archive from a launcher that still took the workspaces: restoring it must not put the old copy
// over the work done since, nor fail on it.
func TestARestorePassesOverWorkspacesAnOlderBackupHolds(t *testing.T) {
	p, _ := workspaceData(t)
	saved := notBackedUp
	notBackedUp = map[string]bool{backupsDirName: true, "upgrade": true, "runtime": true, "launcher.json": true}
	dir, _, err := CreateBackup(p, "desktop-v0.12.0", "native", time.Unix(1_800_000_000, 0), "", nil)
	notBackedUp = saved
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(p.Data, "workspaces", "s1", "main.py"), []byte("print('later')\n"), 0o640)
	os.WriteFile(filepath.Join(p.Data, "state", "checkpoints", "s1", "HEAD"), []byte("later\n"), 0o640)
	if err := RestoreBackup(p, dir, filepath.Join(p.Data, "upgrade", "replaced")); err != nil {
		t.Fatal(err)
	}
	if got := read(t, filepath.Join(p.Data, "workspaces", "s1", "main.py")); got != "print('later')\n" {
		t.Fatalf("the workspace is %q", got)
	}
	if got := read(t, filepath.Join(p.Data, "state", "checkpoints", "s1", "HEAD")); got != "later\n" {
		t.Fatalf("the snapshot store is %q", got)
	}
}

func TestDockerBackupLeavesTheWorkspacesVolumeOut(t *testing.T) {
	backup := t.TempDir()
	var archived, restored []string
	docker := func(ctx context.Context, args ...string) (string, error) {
		switch {
		case args[0] == "image" && args[1] == "inspect":
			return "sha256:0123456789\n", nil
		case args[0] == "volume":
			return "daedalus_daedalus-state\tdaedalus-state\n" +
				"daedalus_daedalus-workspaces\tdaedalus-workspaces\n" +
				"daedalus_daedalus-worktrees\tdaedalus-worktrees\n" +
				"daedalus_daedalus-cache\tdaedalus-cache\n" +
				"daedalus_daedalus-venv\tdaedalus-venv\n" +
				"daedalus_searxng-cache\tsearxng-cache\n" +
				"daedalus_terminals-home\tterminals-home\n", nil
		case args[0] == "run":
			joined := strings.Join(args, " ")
			for _, volume := range []string{"daedalus_daedalus-state", "daedalus_daedalus-workspaces", "daedalus_daedalus-worktrees", "daedalus_daedalus-cache", "daedalus_daedalus-venv", "daedalus_searxng-cache", "daedalus_terminals-home"} {
				switch {
				case strings.Contains(joined, volume+":/from:ro"):
					archived = append(archived, volume)
					os.WriteFile(filepath.Join(backup, backupVolumes, volume+".tar.gz"), makeArchive(t, "v.tar.gz", []archiveEntry{{name: "f", body: "x"}}), 0o600)
				case strings.Contains(joined, volume+":/to"):
					restored = append(restored, volume)
				}
			}
		}
		return "", nil
	}
	manifest := &Manifest{}
	if err := snapshotDocker(context.Background(), backup, manifest, docker); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(archived, ","); got != "daedalus_daedalus-state,daedalus_terminals-home" {
		t.Fatalf("archived %s", got)
	}
	if err := restoreDocker(context.Background(), backup, manifest, docker); err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(restored, ","); got != "daedalus_daedalus-state,daedalus_terminals-home" {
		t.Fatalf("restored %s; a volume the backup left out must keep what it holds", got)
	}
}
