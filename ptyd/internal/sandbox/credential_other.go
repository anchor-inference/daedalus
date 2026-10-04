//go:build !linux

package sandbox

import (
	"errors"
	"os"
)

func SnapshotCredentialFile(_ *os.File, _ string) (*os.File, error) {
	return nil, errors.New("sandbox: credential snapshots require Linux")
}
