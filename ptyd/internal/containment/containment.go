// Package containment owns resource limits for one execution across all of its terminals.
// An attempt without a verified kernel containment handle never claims a whole-tree stop.
package containment

import (
	"errors"
	"time"
)

var ErrUnavailable = errors.New("kernel attempt containment is unavailable")

// Scope is supplied by the authenticated host. The daemon chooses the cgroup path itself.
type Scope struct {
	AttemptID      string `json:"attempt_id"`
	HostGeneration string `json:"host_generation"`
	LaunchID       string `json:"launch_id"`
}

// Limits are hard kernel ceilings. A zero field is unrestricted.
type Limits struct {
	MemoryBytes  int64 `json:"memory_bytes"`
	CPUMillis    int64 `json:"cpu_millis"`
	ProcessCount int64 `json:"process_count"`
	DiskBytes    int64 `json:"disk_bytes"`
}

// Evidence is observed from the same kernel handle that owned the spawned process.
type Evidence struct {
	Kind            string `json:"kind"`
	Enforced        bool   `json:"enforced"`
	Populated       bool   `json:"populated"`
	MemoryPeakBytes int64  `json:"memory_peak_bytes"`
	CPUUsageUsec    int64  `json:"cpu_usage_usec"`
	Processes       int64  `json:"processes"`
	OOMKills        int64  `json:"oom_kills"`
	PIDsMaxEvents   int64  `json:"pids_max_events"`
	Reason          string `json:"reason,omitempty"`
}

// Status is an effective capability observation, not the presence of a cgroup mount.
type Status struct {
	Kind      string `json:"kind"`
	Available bool   `json:"available"`
	Reason    string `json:"reason,omitempty"`
}

// Handle covers the whole attempt, including companion terminals and escaped descendants.
type Handle interface {
	FD() int
	Scope() Scope
	Limits() Limits
	Observe() (Evidence, error)
	Kill(time.Duration) (Evidence, error)
	Close() error
}
