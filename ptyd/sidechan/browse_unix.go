//go:build unix

package sidechan

import "syscall"

// readableDir reports whether this user may list and enter a directory (access(2) with R_OK|X_OK).
func readableDir(p string) bool { return syscall.Access(p, 4|1) == nil }

// volumes is the filesystem's root: one tree, mounts within it.
func volumes() []Place { return []Place{{Name: "/", Path: "/", Kind: "volume"}} }
