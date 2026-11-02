package sidechan

import (
	"os"
	"strconv"
)

// openedPath is the path the kernel has for an open file, read through /proc.
func openedPath(f *os.File) (string, bool) {
	p, err := os.Readlink("/proc/self/fd/" + strconv.Itoa(int(f.Fd())))
	if err != nil || len(p) == 0 || p[0] != '/' {
		return "", false
	}
	return p, true
}
