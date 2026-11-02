package sidechan

import (
	"bytes"
	"os"
	"syscall"
	"unsafe"

	"golang.org/x/sys/unix"
)

// openedPath is the path the kernel has for an open file, from fcntl(F_GETPATH). It is the path
// with every link resolved, as EvalSymlinks gives it (/private/var/..., not /var/...), and through
// the firmlinks macOS presents its data volume with (/Users/..., not /System/Volumes/Data/Users/...),
// which is how the roots it is compared with are resolved too.
func openedPath(f *os.File) (string, bool) {
	buf := make([]byte, unix.PathMax)
	_, _, errno := syscall.Syscall(syscall.SYS_FCNTL, f.Fd(), unix.F_GETPATH, uintptr(unsafe.Pointer(&buf[0])))
	if errno != 0 {
		return "", false
	}
	p := buf[:max(0, bytes.IndexByte(buf, 0))]
	if len(p) == 0 || p[0] != '/' {
		return "", false
	}
	return string(p), true
}
