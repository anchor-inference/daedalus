//go:build windows

package ax

import "os/exec"

// setGroup does nothing on Windows: the tool starts no helper there that outlives its call (its
// cursor overlay is the one exception, and the daemon never enables it).
func setGroup(cmd *exec.Cmd) {}

func killGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return cmd.Process.Kill()
}
