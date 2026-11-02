//go:build !unix

package sidechan

import (
	"errors"
	"os"
)

// privateSocket follows no link here: who owns a socket and its directory is not checked on these
// systems, so a link could name anyone's.
func privateSocket(path string, st os.FileInfo) error {
	return errors.New("a socket behind a link, which is not followed on this system")
}
