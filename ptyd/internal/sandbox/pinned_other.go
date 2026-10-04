//go:build !linux

package sandbox

import (
	"errors"
	"os"
)

func pinReadSources(_ *os.File, _ []ReadSource) ([]*os.File, error) {
	return nil, errors.New("sandbox: pinned source mounts require Linux openat2")
}
