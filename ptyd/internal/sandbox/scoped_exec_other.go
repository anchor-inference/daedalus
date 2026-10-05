//go:build !linux

package sandbox

import "errors"

func RunScopedExec(_ []string) error {
	return errors.New("sandbox: scoped executor is unavailable on this platform")
}
