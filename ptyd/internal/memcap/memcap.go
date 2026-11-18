// Package memcap holds every program the daemon runs on the host, together, under one memory cap
// the operator sets in the app.
//
// Eight agents building and testing a Rust workspace at once took a server from 15 GB to 61 GB;
// nothing was killed, the kernel swapped instead, and every service on the machine stopped answering
// for an hour. A cap on the programs alone lets the kernel kill the largest of them and leaves the
// rest of the machine, the daemon included, running.
//
// On Linux the daemon splits the control group systemd delegated to it into a "daemon" leaf, where
// it lives, and a "jobs" group with the cap, where every program it starts is placed as it is
// created. Without a delegated group it falls back to capping its own group, itself included. On
// Windows the programs share a job object with a job-wide memory limit. Elsewhere there is no cap.
package memcap

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync/atomic"
)

// Request is the cap the host asks for: a share of the machine's memory, a number of bytes, or none.
// Exactly one is set.
type Request struct {
	Percent   float64 `json:"percent,omitempty"`
	Bytes     int64   `json:"bytes,omitempty"`
	Unlimited bool    `json:"unlimited,omitempty"`
}

const (
	// SwapBytes is all the swap the group may use. Little on purpose: a group swapping freely is
	// what froze the server, and a runaway build killed at the cap is the better outcome.
	SwapBytes = int64(2) << 30
	// MinBytes is the smallest cap taken: below it a shell and a compiler cannot start at all.
	MinBytes = int64(256) << 20
)

// Status is what is in force, as the daemon reads it back from the kernel, not what was asked.
// A limit that is null is no limit (a swap limit of 0 is no swap at all); a count of -1 could not be
// read.
type Status struct {
	Supported    bool     `json:"supported"`
	Mode         string   `json:"mode"` // "jobs", "own", "job_object", or "" when nothing can be capped
	Reason       string   `json:"reason,omitempty"`
	Requested    *Request `json:"requested,omitempty"`
	MachineBytes int64    `json:"machine_bytes"`
	MaxBytes     *int64   `json:"max_bytes"`
	HighBytes    *int64   `json:"high_bytes"`
	SwapMaxBytes *int64   `json:"swap_max_bytes"`
	CurrentBytes int64    `json:"current_bytes"`
	OOMKills     int64    `json:"oom_kills"`
}

// Bytes reads a limit of Status: 0 for none.
func Bytes(limit *int64) int64 {
	if limit == nil {
		return 0
	}
	return *limit
}

// Limits are what a request comes to on a machine; 0 is no limit.
//
// High, the throttle point (memory.high), is always none, on measurement: with it at 85% of a
// 256 MB cap and no swap, a program allocating 1 GB sat throttled at 236 MB for the whole 60 seconds
// it was given, 19 716 throttling events and never a kill; at 95% the same for 30 s. Memory a program
// holds cannot be reclaimed without swap, so the throttle never lets go, and a runaway build would
// hang the agent's command for its whole timeout instead of failing. With memory.max alone the same
// program was killed in 261 ms and the command said why. Writing none also clears a throttle point
// an earlier daemon left.
type Limits struct {
	Max, High, Swap int64
}

// Resolve turns a request into limits on a machine with total bytes of memory. A cap larger than the
// machine is held to the machine, and the note says so.
func Resolve(r Request, total int64) (Limits, string, error) {
	set := 0
	if r.Percent != 0 {
		set++
	}
	if r.Bytes != 0 {
		set++
	}
	if r.Unlimited {
		set++
	}
	if set != 1 {
		return Limits{}, "", errors.New("give exactly one of percent, bytes and unlimited")
	}
	if r.Unlimited {
		return Limits{}, "", nil
	}
	var max int64
	note := ""
	switch {
	case r.Percent != 0:
		if r.Percent < 1 || r.Percent > 100 {
			return Limits{}, "", errors.New("percent is between 1 and 100")
		}
		if total <= 0 {
			return Limits{}, "", errors.New("the machine's memory is unknown, so a share of it cannot be set")
		}
		max = int64(float64(total) * r.Percent / 100)
	default:
		if r.Bytes < MinBytes {
			return Limits{}, "", fmt.Errorf("the cap is at least %d MB", MinBytes>>20)
		}
		max = r.Bytes
		if total > 0 && max > total {
			max, note = total, "the cap asked for is more than the machine has, so it is the machine's memory"
		}
	}
	max = max &^ 4095
	if max < MinBytes {
		max = MinBytes
	}
	return Limits{Max: max, Swap: SwapBytes}, note, nil
}

// loadRequest reads the request saved by the last Apply, so a restarted daemon holds its programs
// to the operator's cap before the host has reconnected to send it again.
func loadRequest(path string) *Request {
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var r Request
	if json.Unmarshal(data, &r) != nil {
		return nil
	}
	return &r
}

func saveRequest(path string, r Request) error {
	if path == "" {
		return nil
	}
	data, _ := json.Marshal(r)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

var installed atomic.Pointer[Manager]

// Install makes m the cap every spawn in this process goes under. A daemon calls it once at start.
func Install(m *Manager) { installed.Store(m) }

// Installed is the cap of this process, or nil: tests and the CLI's embedded runner have none.
func Installed() *Manager { return installed.Load() }

// Kills is the count of programs the kernel killed under the cap so far, or -1 when there is no cap
// to count against. exec.run compares it before and after a program.
func Kills() int64 {
	m := Installed()
	if m == nil {
		return -1
	}
	return m.Kills()
}
