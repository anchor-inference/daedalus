//go:build linux

package rpc

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"golang.org/x/sys/unix"
)

func diskPreflight(path string) diskObservation {
	if !filepath.IsAbs(path) {
		return diskObservation{Reason: "selected workspace path is not absolute"}
	}
	probe := path
	exists := true
	for {
		info, err := os.Stat(probe)
		if err == nil {
			if !info.IsDir() {
				return diskObservation{Reason: "selected workspace is not a directory"}
			}
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return diskObservation{Reason: "selected workspace is unavailable"}
		}
		exists = false
		parent := filepath.Dir(probe)
		if parent == probe {
			return diskObservation{Reason: "selected workspace ancestor is unavailable"}
		}
		probe = parent
	}
	var stat unix.Statx_t
	if err := unix.Statx(unix.AT_FDCWD, probe, 0, unix.STATX_BASIC_STATS|unix.STATX_MNT_ID, &stat); err != nil || stat.Mask&unix.STATX_MNT_ID == 0 || stat.Mnt_id == 0 {
		return diskObservation{Reason: "selected workspace mount identity is unavailable"}
	}
	var fs unix.Statfs_t
	if err := unix.Statfs(probe, &fs); err != nil || fs.Bsize <= 0 || fs.Bavail > uint64(1<<63-1)/uint64(fs.Bsize) {
		return diskObservation{Reason: "selected workspace free space is unavailable"}
	}
	sum := sha256.Sum256([]byte(path))
	return diskObservation{Available: true, FreeBytes: int64(fs.Bavail) * fs.Bsize,
		DeviceID: fmt.Sprintf("%d:%d", stat.Dev_major, stat.Dev_minor), MountID: strconv.FormatUint(stat.Mnt_id, 10),
		ObservedAt: time.Now().UTC().Format(time.RFC3339Nano), PathDigest: hex.EncodeToString(sum[:]),
		WorkspaceExists: exists}
}
