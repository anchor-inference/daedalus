//go:build linux

package sandbox

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

// scopedProviderDirectory pins the broker's sole socket. The broker must own this private
// directory until the launch exits; a mutable directory cannot authorize an unknown peer.
func scopedProviderDirectory(source *os.File) (*os.File, error) {
	if source == nil {
		return nil, errors.New("sandbox: provider directory is missing")
	}
	var stat unix.Stat_t
	if err := unix.Fstat(int(source.Fd()), &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFDIR ||
		stat.Uid != uint32(os.Geteuid()) || stat.Mode&0o077 != 0 {
		return nil, errors.New("sandbox: provider directory must be private and owned by this user")
	}
	fd, err := unix.Openat2(int(source.Fd()), ".", &unix.OpenHow{Flags: unix.O_RDONLY | unix.O_DIRECTORY | unix.O_CLOEXEC,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS | unix.RESOLVE_NO_XDEV})
	if err != nil {
		return nil, err
	}
	provider := os.NewFile(uintptr(fd), "provider socket directory")
	entries, err := provider.ReadDir(-1)
	if err != nil || len(entries) != 1 || entries[0].Name() != "provider.sock" {
		provider.Close()
		return nil, errors.New("sandbox: provider directory must contain one socket")
	}
	if err := unix.Fstatat(fd, "provider.sock", &stat, unix.AT_SYMLINK_NOFOLLOW); err != nil ||
		stat.Mode&unix.S_IFMT != unix.S_IFSOCK || stat.Uid != uint32(os.Geteuid()) || stat.Mode&0o077 != 0 {
		provider.Close()
		return nil, errors.New("sandbox: provider endpoint must be a private socket owned by this user")
	}
	return provider, nil
}
