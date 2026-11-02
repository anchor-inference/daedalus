//go:build !unix && !windows

package sidechan

import "os"

func openNoFollow(p string, dir bool) (*os.File, error) { return os.Open(p) }

func openForWrite(p string, create bool) (*os.File, error) {
	if create {
		return os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o666)
	}
	return os.OpenFile(p, os.O_WRONLY, 0)
}

func openedPath(f *os.File) (string, bool) { return "", false }

func fileID(st os.FileInfo) string { return "" }

// writable is unknown here, and left out of the answer rather than guessed.
func writable(p string) *bool { return nil }
