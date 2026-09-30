package main

import (
	"archive/tar"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// In Docker mode the database, the workspaces and the agent's memory are volumes, not files in the
// data folder, so a backup that covered only the folder would miss exactly what a migration changes.
// Each volume of the compose project is archived by a throwaway container of the agent's own image —
// the image is there by definition and has tar — into the backup folder, and restored the same way.
// The image ids the stack ran are recorded too, so a rollback can point :latest and :browser back at
// them. Nothing here has been run against a real Docker installation yet (UPDATES.md), so the upgrade
// refuses Docker mode outright and this is reached only by the unit tests until a restore and a
// health check on disposable volumes and images have been done.

// dockerRunner runs one docker command; the tests replace it.
type dockerRunner func(ctx context.Context, args ...string) (string, error)

func projectVolumes(ctx context.Context, docker dockerRunner) ([]string, error) {
	out, err := docker(ctx, "volume", "ls", "--quiet", "--filter", "label=com.docker.compose.project="+projectName)
	if err != nil {
		return nil, err
	}
	var volumes []string
	for _, line := range strings.Split(out, "\n") {
		if name := strings.TrimSpace(line); name != "" {
			volumes = append(volumes, name)
		}
	}
	return volumes, nil
}

func snapshotDocker(ctx context.Context, backup string, manifest *Manifest, docker dockerRunner) error {
	manifest.Images = map[string]string{}
	for _, ref := range []string{agentImage(), browserImage()} {
		id, err := docker(ctx, "image", "inspect", "--format", "{{.Id}}", ref)
		if err == nil && strings.TrimSpace(id) != "" {
			manifest.Images[ref] = strings.TrimSpace(id)
		}
	}
	tool := manifest.Images[agentImage()]
	if tool == "" {
		return fmt.Errorf("the agent's image %s is not on this machine, and it is what archives the volumes", agentImage())
	}
	volumes, err := projectVolumes(ctx, docker)
	if err != nil {
		return err
	}
	dir := filepath.Join(backup, backupVolumes)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	for _, volume := range volumes {
		archive := sanitizeName(volume) + ".tar.gz"
		if _, err := docker(ctx, volumeArchiveArgs(tool, volume, dir, archive)...); err != nil {
			return fmt.Errorf("volume %s: %w", volume, err)
		}
		name := filepath.Join(dir, archive)
		if err := os.Chmod(name, 0o600); err != nil {
			return err
		}
		sum, _, err := sha256File(name)
		if err != nil {
			return err
		}
		count := 0
		if err := readArchive(name, func(string, *tar.Header, io.Reader) error { count++; return nil }); err != nil {
			return fmt.Errorf("volume %s: the archive does not read back: %w", volume, err)
		}
		manifest.Volumes = append(manifest.Volumes, VolumeBackup{Name: volume, Archive: archive, SHA256: sum, Entries: count})
	}
	return nil
}

// volumeArchiveArgs archives one volume read-only. Root inside the container, because the volumes
// belong to the container's users; the archive is handed back to the launcher's own user.
func volumeArchiveArgs(image, volume, dir, archive string) []string {
	args := []string{"run", "--rm", "--network", "none", "-v", volume + ":/from:ro", "-v", dir + ":/to",
		"--user", "0:0", "--entrypoint", "sh", image, "-c", `tar -czf "/to/$1" -C /from . && chown "$2" "/to/$1"`, "sh", archive, ownerSpec()}
	return args
}

func ownerSpec() string {
	if uid := os.Getuid(); uid >= 0 {
		return fmt.Sprintf("%d:%d", uid, os.Getgid())
	}
	return "0:0"
}

// volumeRestoreArgs empties a volume and unpacks its archive into it.
func volumeRestoreArgs(image, volume, dir, archive string) []string {
	return []string{"run", "--rm", "--network", "none", "-v", volume + ":/to", "-v", dir + ":/from:ro",
		"--user", "0:0", "--entrypoint", "sh", image, "-c", `find /to -mindepth 1 -delete && tar -xzf "/from/$1" -C /to`, "sh", archive}
}

func restoreDocker(ctx context.Context, backup string, manifest *Manifest, docker dockerRunner) error {
	tool := manifest.Images[agentImage()]
	if tool == "" && len(manifest.Volumes) > 0 {
		return fmt.Errorf("the backup does not record the image that archived its volumes")
	}
	dir := filepath.Join(backup, backupVolumes)
	for _, volume := range manifest.Volumes {
		if err := verifyVolumeArchive(backup, volume); err != nil {
			return err
		}
		if _, err := docker(ctx, volumeRestoreArgs(tool, volume.Name, dir, volume.Archive)...); err != nil {
			return fmt.Errorf("volume %s: %w", volume.Name, err)
		}
	}
	for ref, id := range manifest.Images {
		if _, err := docker(ctx, "image", "tag", id, ref); err != nil {
			return fmt.Errorf("could not point %s back at %s: %w", ref, id, err)
		}
	}
	return nil
}
