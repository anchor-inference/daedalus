//go:build !unix

package display

import "os/exec"

func detach(*exec.Cmd) {}
