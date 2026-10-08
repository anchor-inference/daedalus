//go:build unix

package sidechan

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
)

// privateSocket accepts a socket owned by this daemon's user in a directory owned by that user that
// no other user may write: no one else could have put it there, or swap it for another.
func privateSocket(path string, st os.FileInfo) error {
	uid := uint32(os.Getuid())
	if sys, ok := st.Sys().(*syscall.Stat_t); !ok || sys.Uid != uid {
		return errors.New("a socket of another user")
	}
	dir, err := os.Lstat(filepath.Dir(path))
	if err != nil || !dir.IsDir() {
		return errors.New("a socket outside a directory")
	}
	sys, ok := dir.Sys().(*syscall.Stat_t)
	if !ok || sys.Uid != uid || dir.Mode().Perm()&0o022 != 0 {
		return errors.New("a socket in a directory others may write")
	}
	return nil
}
