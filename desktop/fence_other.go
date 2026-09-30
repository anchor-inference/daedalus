//go:build !linux

package main

import (
	"errors"
	"os"
	"time"
)

// Windows and macOS have no read lease that shuts a writer out, so the fenced switch refuses there
// before it touches anything, and updates protect the data with the verified backup (protect.go).
func fencedSwitch(opts fenceOptions) fenceReport {
	now := time.Now().UTC()
	return fenceReport{Data: opts.Data, Outcome: fenceFailClosed, ExitCode: fenceExitCode(fenceFailClosed),
		Reason: "this platform has no kernel writer fence; the data is left as it is", Started: now, Finished: now}
}

type fenceOptions struct {
	Data            string
	Control         string
	BackendInDocker bool
	Held            map[string]*os.File
	Handover        func(map[string]*os.File)
	RestoreFrom     string
	Upgrading       bool
}

// fenceTreeKey: no switch here, so no kept copy to recognise.
func fenceTreeKey(string) ([2]uint64, error) {
	return [2]uint64{}, errors.New("no fence on this platform")
}

// fencePlatform: there is no kernel writer fence here; updates take the verified backup.
func fencePlatform(string) error {
	return errors.New("this platform has no kernel writer fence")
}

// fenceCollectRetained: no kept copies are made here, so there are none to remove.
func fenceCollectRetained(data, control, name string, operator bool, _ any) fenceGCReport {
	return fenceGCReport{Name: name, Outcome: fenceRetainedGC, Reason: "no fence on this platform", At: time.Now().UTC()}
}

func fenceResolve(data, control string, apply bool) ([]fenceResolution, error) { return nil, nil }

type fenceResolution struct {
	Op      string
	Phase   string
	Live    string
	Actions []string
	Applied bool
}
