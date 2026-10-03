//go:build linux

package ptyproc

import (
	"errors"
	"syscall"
)

func containmentAttrs(attrs *syscall.SysProcAttr, spec Spec) error {
	if spec.UseContainment {
		if spec.ContainmentFD <= 0 {
			return errors.New("attempt containment has no owned cgroup handle")
		}
		attrs.UseCgroupFD, attrs.CgroupFD = true, spec.ContainmentFD
	}
	return nil
}
