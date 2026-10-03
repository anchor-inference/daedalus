//go:build darwin

package ptyproc

import "syscall"

func containmentAttrs(_ *syscall.SysProcAttr, spec Spec) error {
	if spec.UseContainment {
		return ErrUnsupported
	}
	return nil
}
