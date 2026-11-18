//go:build !linux && !windows

package memcap

import (
	"errors"
	"fmt"
	"runtime"
)

// Manager on this system caps nothing: macOS has no per-group memory limit an unprivileged daemon
// can set, and a per-process rlimit would neither add the programs up nor be enforced for resident
// memory.
type Manager struct{}

// Setup returns a Manager that says why there is no cap here.
func Setup(_ string) *Manager { return &Manager{} }

func (m *Manager) reason() string {
	return fmt.Sprintf("a memory cap for the host's programs is not supported on %s", runtime.GOOS)
}

func (m *Manager) Apply(_ Request) (Status, error) { return m.Status(), errors.New(m.reason()) }
func (m *Manager) Status() Status                  { return Status{Reason: m.reason()} }
func (m *Manager) Kills() int64                    { return -1 }
func (m *Manager) JobsDir() string                 { return "" }
