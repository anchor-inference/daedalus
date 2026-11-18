//go:build linux

package ptyproc

import (
	"errors"
	"syscall"

	"github.com/ascorblack/daedalus/ptyd/internal/memcap"
)

func containmentAttrs(attrs *syscall.SysProcAttr, spec Spec) error {
	if spec.UseContainment {
		if spec.ContainmentFD <= 0 {
			return errors.New("attempt containment has no owned cgroup handle")
		}
		attrs.UseCgroupFD, attrs.CgroupFD = true, spec.ContainmentFD
		return nil
	}
	// Every other terminal goes under the host commands' memory cap as it is created.
	memcap.Place(attrs)
	return nil
}
