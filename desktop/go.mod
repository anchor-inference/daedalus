module github.com/ascorblack/daedalus/desktop

go 1.23.0

// The kernel calls the data folder's switch is built on (renameat2, file leases, inotify, the
// filesystem flags ioctl) are named here and not in syscall. Held at the last release that still
// builds with Go 1.23, the toolchain every build and gate of the launcher uses; a newer one moves
// the whole module to a newer Go.
require golang.org/x/sys v0.35.0
