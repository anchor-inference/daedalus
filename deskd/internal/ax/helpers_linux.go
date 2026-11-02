//go:build linux

package ax

import (
	"bytes"
	"os"
	"strconv"
	"syscall"
)

// userProcessEnvs reads the environment of every process the user may read. The tool keeps no
// helper on Linux; this serves the tests, which prove the search on a helper they start themselves.
func userProcessEnvs() map[int][]string {
	out := map[int][]string{}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return out
	}
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == os.Getpid() {
			continue
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/environ")
		if err != nil {
			continue
		}
		var env []string
		for _, kv := range bytes.Split(b, []byte{0}) {
			if len(kv) > 0 {
				env = append(env, string(kv))
			}
		}
		out[pid] = env
	}
	return out
}

func killProcess(pid int) { _ = syscall.Kill(pid, syscall.SIGKILL) }
