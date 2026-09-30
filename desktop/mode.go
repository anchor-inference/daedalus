package main

// Two ways to run Daedalus on this machine, and the launcher drives both.
//
// *Docker* puts the agent in a container: the process the agent's shell commands run in is not the
// operator's, it has its own filesystem and its own network, and a command that goes wrong stops at
// the container's edge. It costs Docker Desktop — an application of its own with a virtual machine
// behind it — and about half a gigabyte of images on top.
//
// *Native* runs the same code as a process on the machine, out of a private folder of downloaded
// binaries. It is smaller, it starts in a second instead of waiting for a VM, and it has no
// container boundary: Exec runs as the operator. The policy engine, the approval gates, the egress
// allowlist and the spend caps are all still there — they are in the agent, not in the container —
// but the wall behind them is gone, and the setup page says so rather than leaving it to be
// discovered.
//
// The choice is made once, on the first run, and written next to the data. A script that wants one
// or the other passes --mode; the environment's DAEDALUS_MODE does the same for a shell that sets
// it up once and runs the launcher many times.

import (
	"context"
	"fmt"
	"os"
	"strings"
)

// Mode is which of the two an installation is. The empty mode is "not chosen yet", which is a state
// the first run is really in and not a value anything may be started with.
type Mode string

const (
	ModeUnset  Mode = ""
	ModeDocker Mode = "docker"
	ModeNative Mode = "native"
)

// ParseMode reads a mode the operator wrote — on the command line, in the environment, or in the
// file a previous run left. Anything else is refused by name rather than silently taken as one of
// the two: a typo that starts the wrong kind of installation is not a small mistake.
func ParseMode(value string) (Mode, error) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "":
		return ModeUnset, nil
	case "docker":
		return ModeDocker, nil
	case "native":
		return ModeNative, nil
	default:
		return ModeUnset, fmt.Errorf("--mode takes docker or native, not %q", value)
	}
}

// StoredMode is what a previous run wrote, or the empty mode when this installation has never
// chosen. A file holding something unrecognisable reads as unchosen: the question is asked again,
// which is better than refusing to start over one word in one file.
func StoredMode(p Paths) Mode {
	mode, err := ParseMode(readFile(p.Mode))
	if err != nil {
		return ModeUnset
	}
	return mode
}

// StoreMode records the choice. It is written once and read on every start after that.
func StoreMode(p Paths, mode Mode) error {
	if mode == ModeUnset {
		return nil
	}
	if err := p.EnsureDirs(); err != nil {
		return err
	}
	return os.WriteFile(p.Mode, []byte(string(mode)+"\n"), 0o644)
}

// ResolveMode decides how this run works: what was asked for on the command line, else what the
// environment says, else what this installation chose before. The answer may still be the empty
// mode — a first run with nothing asked for — and it is the caller that then puts the question.
func ResolveMode(p Paths, asked Mode) (Mode, error) {
	if asked != ModeUnset {
		return asked, nil
	}
	if fromEnv, err := ParseMode(os.Getenv("DAEDALUS_MODE")); err != nil {
		return ModeUnset, err
	} else if fromEnv != ModeUnset {
		return fromEnv, nil
	}
	if stored := StoredMode(p); stored != ModeUnset {
		return stored, nil
	}
	if p.Configured() {
		// An installation that already has its environment written was made by a launcher that
		// only had one shape. It is a Docker installation, and it is not asked about it again.
		return ModeDocker, nil
	}
	return ModeUnset, nil
}

// SuggestMode is what the first run offers before the operator says. A machine with a running
// Docker is one where the container boundary costs nothing more to have, so that is the suggestion
// there; a machine without one would have to install a whole application first, and native is the
// honest default.
func SuggestMode(ctx context.Context) Mode {
	if CheckDocker(ctx) == nil {
		return ModeDocker
	}
	return ModeNative
}

// Describe is the one line the status page and the terminal say about the mode in force.
func (m Mode) Describe() string {
	switch m {
	case ModeNative:
		return "native — the agent runs on this machine, out of a runtime the launcher downloads into this user's cache folder; lighter and faster, no container boundary"
	case ModeDocker:
		return "docker — the agent runs in a container with its own filesystem and network"
	default:
		return "not chosen yet"
	}
}
