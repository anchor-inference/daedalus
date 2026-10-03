//go:build linux

package ptyproc

import (
	"syscall"
	"testing"
)

func TestContainmentNeedsAnOwnedHandleBeforeSpawn(t *testing.T) {
	attrs := &syscall.SysProcAttr{}
	if err := containmentAttrs(attrs, Spec{UseContainment: true}); err == nil || attrs.UseCgroupFD {
		t.Fatal("strict start accepted a missing cgroup handle")
	}
	if err := containmentAttrs(attrs, Spec{UseContainment: true, ContainmentFD: 17}); err != nil {
		t.Fatal(err)
	}
	if !attrs.UseCgroupFD || attrs.CgroupFD != 17 {
		t.Fatal("the child would start outside its exact attempt group")
	}
}
