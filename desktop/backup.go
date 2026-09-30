package main

import (
	"archive/tar"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

// A backup is the data folder as it was before an upgrade, taken with the stack stopped: one
// gzipped tar and a manifest that names every entry with its size and SHA-256. The manifest is what
// the backup is checked against twice — once right after it is written, by reading the archive back,
// and once before it is restored — and what the restored tree is checked against afterwards. It
// proves the archive is the copy that was taken; it proves nothing about where a release came from.
//
//	<data>/backups/<UTC time>-<launcher version>/data.tar.gz
//	<data>/backups/<UTC time>-<launcher version>/manifest.json
//	<data>/backups/<UTC time>-<launcher version>/volumes/<volume>.tar.gz   (Docker mode)
//
// The backup holds everything the installation owns, the provider keys included, so the folder is
// 0700 and the files 0600, like the secrets folder they are copied from. What is left out is what
// the launcher downloads again by itself (runtime/), the backups themselves, the upgrade's own
// working folder and the running launcher's handover file.
const (
	backupsDirName  = "backups"
	backupArchive   = "data.tar.gz"
	launcherArchive = "launcher.tar.gz"
	backupManifest  = "manifest.json"
	backupVolumes   = "volumes"
	backupKeep      = 3
	manifestVersion = 1
)

// notBackedUp are the top-level names under the data folder that a backup skips and a restore
// leaves where they are.
var notBackedUp = map[string]bool{
	backupsDirName:  true,
	"upgrade":       true,
	"runtime":       true,
	"launcher.json": true,
}

// BackupEntry is one thing in the archive.
type BackupEntry struct {
	Path   string `json:"path"` // slash-separated, relative to the data folder
	Kind   string `json:"kind"` // file, dir or symlink
	Mode   uint32 `json:"mode"`
	Size   int64  `json:"size,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
	Target string `json:"target,omitempty"`
}

// VolumeBackup is one Docker volume, archived by a container into the backup folder.
type VolumeBackup struct {
	Name    string `json:"name"`
	Archive string `json:"archive"`
	SHA256  string `json:"sha256"`
	Entries int    `json:"entries"`
}

// Manifest describes a backup.
type Manifest struct {
	Version       int            `json:"version"`
	Created       time.Time      `json:"created"`
	Launcher      string         `json:"launcher"`
	Mode          string         `json:"mode"`
	Data          string         `json:"data"`
	ArchiveSHA256 string         `json:"archive_sha256"`
	Entries       []BackupEntry  `json:"entries"`
	Volumes       []VolumeBackup `json:"volumes,omitempty"`
	// Images are the Docker images the stack ran before the upgrade, by reference and id, so a
	// rollback can point the references back at them. Local tags only; nothing is pushed.
	Images map[string]string `json:"images,omitempty"`

	// The launcher's own files, when an upgrade is about to replace them: the items under
	// LauncherRoot that the release replaces, archived and checked like the data. The rename that
	// keeps them in old/ is what a rollback uses first; this is what it falls back on.
	LauncherRoot    string        `json:"launcher_root,omitempty"`
	LauncherSHA256  string        `json:"launcher_sha256,omitempty"`
	LauncherEntries []BackupEntry `json:"launcher_entries,omitempty"`
}

func backupsDir(p Paths) string { return filepath.Join(p.Data, backupsDirName) }

// sha256File hashes a file on disk.
func sha256File(name string) (string, int64, error) {
	file, err := os.Open(name)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hash := sha256.New()
	n, err := io.Copy(hash, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(hash.Sum(nil)), n, nil
}

// CreateBackup writes a backup of the data folder into a new folder under <data>/backups and
// returns its path. It is written under a .partial name and renamed only when complete, so a backup
// that was cut off is never mistaken for one that was not.
//
// launcherRoot and launcherItems, when given, name the launcher's files an upgrade is about to
// replace; they are archived beside the data, into launcher.tar.gz.
func CreateBackup(p Paths, launcher, mode string, now time.Time, launcherRoot string, launcherItems []string) (string, *Manifest, error) {
	if err := os.MkdirAll(backupsDir(p), 0o700); err != nil {
		return "", nil, err
	}
	name := now.UTC().Format("20060102T150405Z") + "-" + sanitizeName(launcher)
	final := filepath.Join(backupsDir(p), name)
	// Two backups in one second — an update right after an upgrade — get a number, never the same
	// folder: a backup is not written over another.
	for n := 2; exists(final) || exists(final+".partial"); n++ {
		final = filepath.Join(backupsDir(p), fmt.Sprintf("%s-%d", name, n))
	}
	partial := final + ".partial"
	if err := os.Mkdir(partial, 0o700); err != nil {
		return "", nil, err
	}
	manifest, err := writeArchive(p.Data, filepath.Join(partial, backupArchive), func(top string) bool { return !notBackedUp[top] })
	if err != nil {
		os.RemoveAll(partial)
		return "", nil, fmt.Errorf("the backup could not be written: %w", err)
	}
	if len(launcherItems) > 0 {
		wanted := map[string]bool{}
		for _, item := range launcherItems {
			wanted[item] = true
		}
		files, err := writeArchive(launcherRoot, filepath.Join(partial, launcherArchive), func(top string) bool { return wanted[top] })
		if err != nil {
			os.RemoveAll(partial)
			return "", nil, fmt.Errorf("the launcher's files could not be backed up: %w", err)
		}
		manifest.LauncherRoot, manifest.LauncherSHA256, manifest.LauncherEntries = launcherRoot, files.ArchiveSHA256, files.Entries
	}
	manifest.Created, manifest.Launcher, manifest.Mode, manifest.Data = now.UTC(), launcher, mode, p.Data
	if err := writeManifest(partial, manifest); err != nil {
		os.RemoveAll(partial)
		return "", nil, err
	}
	if err := os.Rename(partial, final); err != nil {
		os.RemoveAll(partial)
		return "", nil, err
	}
	return final, manifest, nil
}

func writeManifest(dir string, manifest *Manifest) error {
	body, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	return writeFileSync(filepath.Join(dir, backupManifest), body, 0o600)
}

// writeFileSync writes and syncs, so a manifest or journal that says a step is done is on the disk
// before the step after it starts.
func writeFileSync(name string, body []byte, mode os.FileMode) error {
	tmp := name + ".tmp"
	file, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	if _, err := file.Write(body); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, name)
}

func sanitizeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '.', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

// writeArchive tars root into target, hashing each file as it goes into the archive, so the
// manifest describes exactly the bytes that were written. include decides by top-level name.
func writeArchive(root, target string, include func(top string) bool) (*Manifest, error) {
	out, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	archiveHash := sha256.New()
	zipped := gzip.NewWriter(io.MultiWriter(out, archiveHash))
	writer := tar.NewWriter(zipped)
	manifest := &Manifest{Version: manifestVersion}
	walkErr := filepath.WalkDir(root, func(name string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if name == root {
			return nil
		}
		relative, err := filepath.Rel(root, name)
		if err != nil {
			return err
		}
		slash := filepath.ToSlash(relative)
		top, _, _ := strings.Cut(slash, "/")
		if !include(top) {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		record := BackupEntry{Path: slash, Mode: uint32(info.Mode().Perm())}
		header := &tar.Header{Name: slash, Mode: int64(info.Mode().Perm()), ModTime: info.ModTime()}
		switch {
		case info.Mode().IsDir():
			record.Kind, header.Typeflag, header.Name = "dir", tar.TypeDir, slash+"/"
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(name)
			if err != nil {
				return err
			}
			record.Kind, record.Target = "symlink", filepath.ToSlash(link)
			header.Typeflag, header.Linkname = tar.TypeSymlink, record.Target
		case info.Mode().IsRegular():
			record.Kind, header.Typeflag, header.Size = "file", tar.TypeReg, info.Size()
		default:
			// A socket, a pipe: the supervisor's socket is one. Nothing a restore could bring back.
			return nil
		}
		if err := writer.WriteHeader(header); err != nil {
			return err
		}
		if record.Kind == "file" {
			file, err := os.Open(name)
			if err != nil {
				return err
			}
			hash := sha256.New()
			n, err := io.Copy(io.MultiWriter(writer, hash), file)
			file.Close()
			if err != nil {
				return err
			}
			if n != info.Size() {
				return fmt.Errorf("%s changed size while it was being backed up", slash)
			}
			record.Size, record.SHA256 = n, hex.EncodeToString(hash.Sum(nil))
		}
		manifest.Entries = append(manifest.Entries, record)
		return nil
	})
	closeErr := errors.Join(writer.Close(), zipped.Close(), out.Sync(), out.Close())
	if walkErr != nil {
		return nil, walkErr
	}
	if closeErr != nil {
		return nil, closeErr
	}
	manifest.ArchiveSHA256 = hex.EncodeToString(archiveHash.Sum(nil))
	return manifest, nil
}

// ReadManifest loads a backup's manifest.
func ReadManifest(dir string) (*Manifest, error) {
	body, err := os.ReadFile(filepath.Join(dir, backupManifest))
	if err != nil {
		return nil, err
	}
	var manifest Manifest
	if err := json.Unmarshal(body, &manifest); err != nil {
		return nil, fmt.Errorf("%s: %w", backupManifest, err)
	}
	if manifest.Version != manifestVersion {
		return nil, fmt.Errorf("%s: version %d is not one this launcher reads", backupManifest, manifest.Version)
	}
	return &manifest, nil
}

// cleanEntryName refuses an archive name that would leave the folder it is unpacked into.
func cleanEntryName(name string) (string, error) {
	trimmed := strings.TrimSuffix(name, "/")
	if trimmed == "" || strings.HasPrefix(trimmed, "/") || path.Clean(trimmed) != trimmed ||
		trimmed == ".." || strings.HasPrefix(trimmed, "../") || filepath.IsAbs(filepath.FromSlash(trimmed)) {
		return "", fmt.Errorf("archive entry %q is not a plain relative path", name)
	}
	// On Windows a backslash is a separator and a colon a drive or a stream; elsewhere both are
	// ordinary characters a file under workspaces/ may well have.
	if runtime.GOOS == "windows" && strings.ContainsAny(trimmed, `\:`) {
		return "", fmt.Errorf("archive entry %q is not a plain relative path", name)
	}
	return trimmed, nil
}

// VerifyBackup reads a backup back and checks it against its manifest: the archive's own hash, and
// then every entry — present, of the recorded kind, the recorded size and SHA-256 — and nothing in
// the archive that the manifest does not name. The volume archives are checked by hash and by
// reading them to the end.
func VerifyBackup(dir string) (*Manifest, error) {
	manifest, err := ReadManifest(dir)
	if err != nil {
		return nil, err
	}
	if err := verifyArchive(filepath.Join(dir, backupArchive), manifest.ArchiveSHA256, manifest.Entries); err != nil {
		return nil, err
	}
	if manifest.LauncherSHA256 != "" {
		if err := verifyArchive(filepath.Join(dir, launcherArchive), manifest.LauncherSHA256, manifest.LauncherEntries); err != nil {
			return nil, fmt.Errorf("the launcher's backup: %w", err)
		}
	}
	for _, volume := range manifest.Volumes {
		if err := verifyVolumeArchive(dir, volume); err != nil {
			return nil, err
		}
	}
	return manifest, nil
}

// verifyArchive checks one archive against its hash and its entries.
func verifyArchive(archive, archiveSHA256 string, entries []BackupEntry) error {
	sum, _, err := sha256File(archive)
	if err != nil {
		return err
	}
	if sum != archiveSHA256 {
		return errors.New("the backup archive does not match its manifest")
	}
	expected := map[string]BackupEntry{}
	for _, entry := range entries {
		if _, err := cleanEntryName(entry.Path); err != nil {
			return err
		}
		expected[entry.Path] = entry
	}
	seen := 0
	err = readArchive(archive, func(name string, header *tar.Header, body io.Reader) error {
		entry, ok := expected[name]
		if !ok {
			return fmt.Errorf("the archive holds %q, which the manifest does not name", name)
		}
		switch entry.Kind {
		case "dir":
			if header.Typeflag != tar.TypeDir {
				return fmt.Errorf("%s: expected a directory", name)
			}
		case "symlink":
			if header.Typeflag != tar.TypeSymlink || header.Linkname != entry.Target {
				return fmt.Errorf("%s: expected a link to %s", name, entry.Target)
			}
		case "file":
			if header.Typeflag != tar.TypeReg {
				return fmt.Errorf("%s: expected a file", name)
			}
			hash := sha256.New()
			n, err := io.Copy(hash, body)
			if err != nil {
				return err
			}
			if n != entry.Size || hex.EncodeToString(hash.Sum(nil)) != entry.SHA256 {
				return fmt.Errorf("%s: its content does not match the manifest", name)
			}
		default:
			return fmt.Errorf("%s: unknown kind %q", name, entry.Kind)
		}
		seen++
		return nil
	})
	if err != nil {
		return err
	}
	if seen != len(expected) {
		return fmt.Errorf("the archive holds %d of the %d entries its manifest names", seen, len(expected))
	}
	return nil
}

func verifyVolumeArchive(dir string, volume VolumeBackup) error {
	if strings.ContainsAny(volume.Archive, `/\`) || volume.Archive == "" {
		return fmt.Errorf("volume %s: archive name %q is not a plain file name", volume.Name, volume.Archive)
	}
	name := filepath.Join(dir, backupVolumes, volume.Archive)
	sum, _, err := sha256File(name)
	if err != nil {
		return fmt.Errorf("volume %s: %w", volume.Name, err)
	}
	if sum != volume.SHA256 {
		return fmt.Errorf("volume %s: the archive does not match its manifest", volume.Name)
	}
	count := 0
	if err := readArchive(name, func(string, *tar.Header, io.Reader) error { count++; return nil }); err != nil {
		return fmt.Errorf("volume %s: %w", volume.Name, err)
	}
	if count != volume.Entries {
		return fmt.Errorf("volume %s: %d entries, the manifest says %d", volume.Name, count, volume.Entries)
	}
	return nil
}

// readArchive walks a gzipped tar, handing each entry to visit with a name already checked to be a
// plain relative path. "." — which a tar made with -C dir . starts with — is skipped.
func readArchive(name string, visit func(name string, header *tar.Header, body io.Reader) error) error {
	file, err := os.Open(name)
	if err != nil {
		return err
	}
	defer file.Close()
	zipped, err := gzip.NewReader(file)
	if err != nil {
		return err
	}
	defer zipped.Close()
	reader := tar.NewReader(zipped)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		raw := strings.TrimPrefix(header.Name, "./")
		if raw == "" || raw == "." {
			continue
		}
		clean, err := cleanEntryName(raw)
		if err != nil {
			return err
		}
		if err := visit(clean, header, reader); err != nil {
			return err
		}
	}
}

// RestoreBackup puts the data folder back the way the backup found it. The backup is verified
// first; what the folder holds now is not deleted but moved aside into `aside`, so a restore can
// itself be undone by hand. The restored tree is checked against the manifest before this returns.
func RestoreBackup(p Paths, dir, aside string) error {
	manifest, err := VerifyBackup(dir)
	if err != nil {
		return fmt.Errorf("the backup at %s did not verify, so nothing was restored: %w", dir, err)
	}
	if err := os.MkdirAll(aside, 0o700); err != nil {
		return err
	}
	current, err := os.ReadDir(p.Data)
	if err != nil {
		return err
	}
	var moved []string
	for _, entry := range current {
		if notBackedUp[entry.Name()] {
			continue
		}
		if err := os.Rename(filepath.Join(p.Data, entry.Name()), filepath.Join(aside, entry.Name())); err != nil {
			// Put back what was moved, so a failure here leaves the folder as it was.
			for _, name := range moved {
				_ = os.Rename(filepath.Join(aside, name), filepath.Join(p.Data, name))
			}
			return fmt.Errorf("could not move %s aside: %w", entry.Name(), err)
		}
		moved = append(moved, entry.Name())
	}
	if err := extractBackup(filepath.Join(dir, backupArchive), p.Data); err != nil {
		return fmt.Errorf("the backup could not be unpacked (what was there before is in %s): %w", aside, err)
	}
	if err := checkRestored(p.Data, manifest); err != nil {
		return fmt.Errorf("the restored folder does not match the backup (what was there before is in %s): %w", aside, err)
	}
	return nil
}

func extractBackup(archive, root string) error {
	return extractArchive(archive, root, func(top string) bool { return !notBackedUp[top] })
}

// extractArchive unpacks one of the backup's archives into root; allowed says which top-level
// names it may hold.
func extractArchive(archive, root string, allowed func(top string) bool) error {
	type dirMode struct {
		name string
		mode os.FileMode
	}
	var dirs []dirMode
	err := readArchive(archive, func(name string, header *tar.Header, body io.Reader) error {
		top, _, _ := strings.Cut(name, "/")
		if !allowed(top) {
			return fmt.Errorf("the backup holds %q, which a backup never does", name)
		}
		target, err := safeJoin(root, name)
		if err != nil {
			return err
		}
		mode := os.FileMode(header.Mode).Perm()
		switch header.Typeflag {
		case tar.TypeDir:
			if err := ensureParents(root, target); err != nil {
				return err
			}
			dirs = append(dirs, dirMode{target, mode})
		case tar.TypeReg:
			if err := ensureParents(root, filepath.Dir(target)); err != nil {
				return err
			}
			file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode|0o200)
			if err != nil {
				return err
			}
			if _, err := io.Copy(file, body); err != nil {
				file.Close()
				return err
			}
			if err := file.Close(); err != nil {
				return err
			}
			if err := os.Chmod(target, mode); err != nil {
				return err
			}
		case tar.TypeSymlink:
			// A link in the data folder was there when the backup was taken — node_modules/.bin is
			// full of them — and is put back as it was. It is never followed while unpacking:
			// ensureParents refuses to write through one.
			if err := ensureParents(root, filepath.Dir(target)); err != nil {
				return err
			}
			if err := os.Symlink(filepath.FromSlash(header.Linkname), target); err != nil {
				return err
			}
		default:
			return fmt.Errorf("archive entry %q is of a type a backup never holds", name)
		}
		return nil
	})
	if err != nil {
		return err
	}
	// Directory modes last: a read-only directory would refuse the files that go inside it.
	for i := len(dirs) - 1; i >= 0; i-- {
		if err := os.Chmod(dirs[i].name, dirs[i].mode); err != nil {
			return err
		}
	}
	return nil
}

// checkRestored compares the folder with the manifest, entry by entry.
func checkRestored(root string, manifest *Manifest) error {
	for _, entry := range manifest.Entries {
		name := filepath.Join(root, filepath.FromSlash(entry.Path))
		info, err := os.Lstat(name)
		if err != nil {
			return err
		}
		switch entry.Kind {
		case "dir":
			if !info.IsDir() {
				return fmt.Errorf("%s is not a directory", entry.Path)
			}
		case "symlink":
			link, err := os.Readlink(name)
			if err != nil || filepath.ToSlash(link) != entry.Target {
				return fmt.Errorf("%s does not link to %s", entry.Path, entry.Target)
			}
		case "file":
			sum, size, err := sha256File(name)
			if err != nil {
				return err
			}
			if size != entry.Size || sum != entry.SHA256 {
				return fmt.Errorf("%s does not match the backup", entry.Path)
			}
		}
	}
	return nil
}

// PruneBackups keeps the newest `keep` complete backups and removes the older ones, and any
// .partial left by a backup that was cut off. It runs only after an upgrade has succeeded.
func PruneBackups(p Paths, keep int) error {
	entries, err := os.ReadDir(backupsDir(p))
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	var complete []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if strings.HasSuffix(entry.Name(), ".partial") {
			_ = os.RemoveAll(filepath.Join(backupsDir(p), entry.Name()))
			continue
		}
		complete = append(complete, entry.Name())
	}
	sort.Strings(complete) // the names start with a UTC timestamp
	for len(complete) > keep {
		if err := os.RemoveAll(filepath.Join(backupsDir(p), complete[0])); err != nil {
			return err
		}
		complete = complete[1:]
	}
	return nil
}

// errNotInBackup is RestoreLauncherItem's answer for an item that was not there before the upgrade
// — one the release added — which a rollback has nothing to put back for.
var errNotInBackup = errors.New("not in the backup")

// RestoreLauncherItem puts one of the launcher's top-level items back from the backup into root —
// the fallback for a rollback that finds the renamed copy in old/ gone. The archive is verified
// first and the item must not exist at the destination.
func RestoreLauncherItem(dir, root, item string) error {
	manifest, err := ReadManifest(dir)
	if err != nil {
		return err
	}
	found := false
	for _, entry := range manifest.LauncherEntries {
		if entry.Path == item {
			found = true
		}
	}
	if manifest.LauncherSHA256 == "" || !found {
		return errNotInBackup
	}
	if err := verifyArchive(filepath.Join(dir, launcherArchive), manifest.LauncherSHA256, manifest.LauncherEntries); err != nil {
		return err
	}
	if _, err := os.Lstat(filepath.Join(root, item)); err == nil {
		return fmt.Errorf("%s is in the way", filepath.Join(root, item))
	}
	staging, err := os.MkdirTemp(root, ".daedalus-restore-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(staging)
	if err := extractArchive(filepath.Join(dir, launcherArchive), staging, func(string) bool { return true }); err != nil {
		return err
	}
	if _, err := os.Lstat(filepath.Join(staging, item)); err != nil {
		return fmt.Errorf("the launcher's backup has no %s", item)
	}
	return os.Rename(filepath.Join(staging, item), filepath.Join(root, item))
}

// backupRoom is the space a backup needs, checked before one is written: the size of what it
// copies, plus the same again for a restore to unpack beside the data it moves aside, plus a margin.
// Compression usually makes the archive smaller; this does not count on it.
func backupRoom(p Paths, launcherRoot string, launcherItems []string) (need uint64, err error) {
	add := func(root string, include func(string) bool) error {
		return filepath.WalkDir(root, func(name string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			relative, _ := filepath.Rel(root, name)
			top, _, _ := strings.Cut(filepath.ToSlash(relative), "/")
			if name != root && !include(top) {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			if entry.Type().IsRegular() {
				if info, err := entry.Info(); err == nil {
					need += uint64(info.Size())
				}
			}
			return nil
		})
	}
	if err := add(p.Data, func(top string) bool { return !notBackedUp[top] }); err != nil {
		return 0, err
	}
	if len(launcherItems) > 0 {
		wanted := map[string]bool{}
		for _, item := range launcherItems {
			wanted[item] = true
		}
		if err := add(launcherRoot, func(top string) bool { return wanted[top] }); err != nil {
			return 0, err
		}
	}
	return 2*need + 64<<20, nil
}

// checkRoom refuses a backup the disk has no room for, before a byte of it is written.
func checkRoom(p Paths, launcherRoot string, launcherItems []string) error {
	need, err := backupRoom(p, launcherRoot, launcherItems)
	if err != nil {
		return err
	}
	free, err := freeBytes(p.Data)
	if err != nil {
		return fmt.Errorf("could not tell how much room the disk has: %w", err)
	}
	if free < need {
		return fmt.Errorf("the disk holding %s has %d MB free; the backup and a possible restore need about %d MB", p.Data, free>>20, need>>20)
	}
	return nil
}
