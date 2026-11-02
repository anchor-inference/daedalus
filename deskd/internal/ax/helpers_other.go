//go:build !linux && !darwin

package ax

// The tool keeps no helper process on Windows.
func userProcessEnvs() map[int][]string { return nil }

func killProcess(pid int) {}
