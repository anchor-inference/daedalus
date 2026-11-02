package app

import (
	"errors"
	"net"
	"net/url"

	"github.com/ascorblack/daedalus/cli/internal/auth"
	"github.com/ascorblack/daedalus/cli/internal/client"
)

// ErrNotPaired is a profile with no token: the device was never paired, or logged out.
var ErrNotPaired = errors.New("this machine is not paired with the server: run daedalus-cli login <server>")

// ExitCodeFor is the headless exit code for a failure before or outside a turn.
func ExitCodeFor(err error) int {
	var netErr net.Error
	var urlErr *url.Error
	var opErr *net.OpError
	switch {
	case err == nil:
		return ExitOK
	case errors.Is(err, ErrNotPaired), errors.Is(err, auth.ErrNotFound), client.IsUnauthorized(err):
		return ExitAuth
	case errors.As(err, &opErr), errors.As(err, &netErr), errors.As(err, &urlErr):
		return ExitUnreachable
	}
	return ExitFailed
}
