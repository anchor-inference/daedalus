//go:build linux

package sandbox

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"golang.org/x/sys/unix"
)

const maxCredentialBytes = 16 << 20

// SnapshotCredentialFile copies one private, singly linked file below an already authorized
// directory into a sealed anonymous file. The caller owns the returned descriptor. This only
// verifies local source shape and freezes bytes; authorization of the root and read isolation of
// the launched process remain the caller's responsibility.
func SnapshotCredentialFile(root *os.File, relative string) (*os.File, error) {
	if root == nil || !validCredentialRelative(relative) {
		return nil, errors.New("sandbox: credential needs an open root and a clean relative file name")
	}
	rootStat, err := credentialStat(int(root.Fd()))
	if err != nil || !privateCredentialDirectory(rootStat) {
		return nil, errors.New("sandbox: credential root must be a private directory owned by this user")
	}
	parts := strings.Split(relative, "/")
	parent := int(root.Fd())
	var directories []int
	defer func() {
		for _, fd := range directories {
			unix.Close(fd)
		}
	}()
	for _, part := range parts[:len(parts)-1] {
		fd, openErr := unix.Openat2(parent, part, &unix.OpenHow{Flags: unix.O_PATH | unix.O_DIRECTORY | unix.O_CLOEXEC,
			Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS | unix.RESOLVE_NO_XDEV})
		if openErr != nil {
			return nil, fmt.Errorf("sandbox: credential parent: %w", openErr)
		}
		directories = append(directories, fd)
		stat, statErr := credentialStat(fd)
		if statErr != nil || !privateCredentialDirectory(stat) || stat.Dev != rootStat.Dev {
			return nil, errors.New("sandbox: credential parent must be a private directory owned by this user")
		}
		parent = fd
	}
	fd, err := unix.Openat2(parent, parts[len(parts)-1], &unix.OpenHow{Flags: unix.O_RDONLY | unix.O_CLOEXEC | unix.O_NOFOLLOW,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS | unix.RESOLVE_NO_XDEV})
	if err != nil {
		return nil, fmt.Errorf("sandbox: opening credential: %w", err)
	}
	source := os.NewFile(uintptr(fd), "credential source")
	defer source.Close()
	before, err := credentialStat(fd)
	if err != nil || !privateCredentialFile(before) || before.Dev != rootStat.Dev || before.Size > maxCredentialBytes {
		return nil, errors.New("sandbox: credential must be a private singly linked regular file within the size limit")
	}
	// A second read catches ordinary concurrent edits. No read of a mutable source proves who
	// authored its bytes; the caller must control the directory before requesting the snapshot.
	first, err := io.ReadAll(io.LimitReader(source, maxCredentialBytes+1))
	if err != nil || len(first) > maxCredentialBytes {
		return nil, errors.New("sandbox: cannot read credential within the size limit")
	}
	if _, err = source.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	second, err := io.ReadAll(io.LimitReader(source, maxCredentialBytes+1))
	if err != nil || !bytes.Equal(first, second) {
		return nil, errors.New("sandbox: credential changed while being read")
	}
	after, err := credentialStat(fd)
	if err != nil || !sameCredentialStat(before, after) {
		return nil, errors.New("sandbox: credential changed while being read")
	}
	sealed, err := unix.MemfdCreate("selected-credential", unix.MFD_CLOEXEC|unix.MFD_ALLOW_SEALING)
	if err != nil {
		return nil, err
	}
	snapshot := os.NewFile(uintptr(sealed), "selected credential snapshot")
	if _, err := snapshot.Write(first); err != nil {
		snapshot.Close()
		return nil, err
	}
	if _, err := unix.FcntlInt(snapshot.Fd(), unix.F_ADD_SEALS, unix.F_SEAL_WRITE|unix.F_SEAL_GROW|unix.F_SEAL_SHRINK|unix.F_SEAL_SEAL); err != nil {
		snapshot.Close()
		return nil, err
	}
	if _, err := snapshot.Seek(0, io.SeekStart); err != nil {
		snapshot.Close()
		return nil, err
	}
	return snapshot, nil
}

func validCredentialRelative(path string) bool {
	if path == "" || strings.HasPrefix(path, "/") || strings.Contains(path, "\\") {
		return false
	}
	for _, part := range strings.Split(path, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}

func credentialStat(fd int) (unix.Stat_t, error) {
	var stat unix.Stat_t
	err := unix.Fstat(fd, &stat)
	return stat, err
}

func privateCredentialDirectory(stat unix.Stat_t) bool {
	return stat.Mode&unix.S_IFMT == unix.S_IFDIR && stat.Uid == uint32(os.Geteuid()) && stat.Mode&0o077 == 0
}

func privateCredentialFile(stat unix.Stat_t) bool {
	return stat.Mode&unix.S_IFMT == unix.S_IFREG && stat.Uid == uint32(os.Geteuid()) && stat.Mode&0o077 == 0 && stat.Nlink == 1
}

func sameCredentialStat(a, b unix.Stat_t) bool {
	return a.Dev == b.Dev && a.Ino == b.Ino && a.Nlink == b.Nlink && a.Uid == b.Uid && a.Mode == b.Mode &&
		a.Size == b.Size && a.Mtim == b.Mtim && a.Ctim == b.Ctim
}
