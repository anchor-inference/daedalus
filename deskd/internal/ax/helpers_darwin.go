//go:build darwin

package ax

import (
	"bytes"
	"encoding/binary"
	"os"
	"syscall"

	"golang.org/x/sys/unix"
)

// userProcessEnvs reads the environment of every process of the user, through kern.procargs2, which
// the system answers for a process of the same user only.
func userProcessEnvs() map[int][]string {
	out := map[int][]string{}
	procs, err := unix.SysctlKinfoProcSlice("kern.proc.uid", os.Getuid())
	if err != nil {
		return out
	}
	for _, p := range procs {
		pid := int(p.Proc.P_pid)
		if pid <= 0 || pid == os.Getpid() {
			continue
		}
		raw, err := unix.SysctlRaw("kern.procargs2", pid)
		if err != nil {
			continue
		}
		out[pid] = procargsEnv(raw)
	}
	return out
}

// procargsEnv reads the environment out of kern.procargs2's answer: the argument count, the
// executable's path, NUL padding, the arguments, then the environment, each NUL-terminated.
func procargsEnv(raw []byte) []string {
	if len(raw) < 4 {
		return nil
	}
	argc := int(binary.LittleEndian.Uint32(raw[:4]))
	rest := raw[4:]
	i := bytes.IndexByte(rest, 0)
	if i < 0 {
		return nil
	}
	rest = rest[i:]
	for len(rest) > 0 && rest[0] == 0 {
		rest = rest[1:]
	}
	fields := bytes.Split(rest, []byte{0})
	if len(fields) < argc {
		return nil
	}
	var env []string
	for _, f := range fields[argc:] {
		if len(f) == 0 {
			break
		}
		env = append(env, string(f))
	}
	return env
}

func killProcess(pid int) { _ = syscall.Kill(pid, syscall.SIGKILL) }
