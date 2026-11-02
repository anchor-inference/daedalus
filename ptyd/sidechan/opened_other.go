//go:build unix && !linux && !darwin

package sidechan

import "os"

// openedPath is unknown on other systems; there the check made before the open stands alone.
func openedPath(f *os.File) (string, bool) { return "", false }
