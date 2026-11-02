//go:build windows

package tether

import (
	"io"
)

// Tether is nothing on Windows: each command's job object already dies with the client (package
// doc), so there is no watchdog to talk to.
type Tether struct{}

// Start returns nil: nothing to start.
func Start() (*Tether, error) { return nil, nil }

// Hold does nothing.
func (t *Tether) Hold(int) func() { return func() {} }

// Close does nothing.
func (t *Tether) Close() {}

// Watch is never run on Windows; Child is never true there because Start starts nothing.
func Watch(io.Reader) int { return 0 }
