package sidechan

import "syscall"

// parentDeathPerThread says the parent-death signal is bound to the starting thread (Run pins it).
const parentDeathPerThread = true

// parentDeath asks the kernel to kill the program the moment the process that started it dies,
// however it died. It reaches the leader only; the rest of its group is the tether's to end.
func parentDeath(attrs *syscall.SysProcAttr) { attrs.Pdeathsig = syscall.SIGKILL }
