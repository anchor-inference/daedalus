package sidechan

import (
	"errors"
	"fmt"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/internal/hooks"
)

// validSocket is what a socket in a launch's dial directory may be called: one name, never a path,
// so `unix:` can never reach outside that directory.
var validSocket = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// Dialer opens net.dial streams for launches.
type Dialer struct {
	launches *hooks.Registry
	slots    chan struct{}
}

// NewDialer returns a dialer over the launch registry.
func NewDialer(launches *hooks.Registry) *Dialer {
	return &Dialer{launches: launches, slots: make(chan struct{}, config.MaxDials)}
}

// ErrStaleLaunch is a dial for a launch that is not registered (or has ended).
var ErrStaleLaunch = fmt.Errorf("%w: no such launch", ErrNotFound)

// Dial connects to target for the launch:
//
//   - `unix:<name>` is a socket in the launch's dial directory, which only its programs are told of;
//   - `tcp:127.0.0.1:<port>` is a loopback port the launch registered.
//
// Natively the loopback interface also holds the Daedalus API and the key proxy; only a registered
// port keeps an adapter from reaching them through the daemon. The returned release must be called
// once the stream has ended; the launch closes the connection itself when it ends first.
func (d *Dialer) Dial(target, launchID string) (net.Conn, *hooks.Launch, func(), error) {
	l, ok := d.launches.Get(launchID)
	if !ok {
		return nil, nil, nil, ErrStaleLaunch
	}
	var network, address string
	switch {
	case strings.HasPrefix(target, "unix:"):
		name := strings.TrimPrefix(target, "unix:")
		if !validSocket.MatchString(name) || name == "." || name == ".." {
			return nil, nil, nil, fmt.Errorf("%w: unix:<name> names a socket in the launch's dial directory", ErrInvalid)
		}
		address = filepath.Join(l.DialDir, name)
		st, err := os.Lstat(address)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("%w: no socket %s", ErrNotFound, name)
		}
		// The launch's programs can write where the socket lives. A dial directory they replaced by
		// a link would send the host's stream to whatever socket the link names instead.
		want := d.launches.RealDialPath(l, name)
		if real, err := filepath.EvalSymlinks(filepath.Dir(address)); err != nil || real != filepath.Dir(want) {
			return nil, nil, nil, fmt.Errorf("%w: %s is not in the launch's dial directory", ErrForbidden, name)
		}
		switch {
		case st.Mode()&fs.ModeSocket != 0:
		case st.Mode()&fs.ModeSymlink != 0:
			// Codex from 0.157 on binds its app server's socket in a short private directory of its
			// own and leaves a link at the path it was told to listen on; refusing every link made
			// each Codex launch wait on a socket the host could never reach. A link is followed only
			// to a socket of this daemon's user in a directory nobody else may write.
			address, err = d.linkedSocket(l, name, address)
			if err != nil {
				return nil, nil, nil, err
			}
		default:
			return nil, nil, nil, fmt.Errorf("%w: %s is not a socket", ErrForbidden, name)
		}
		if len(address) > 104 {
			return nil, nil, nil, fmt.Errorf("%w: %s is longer than a unix socket path may be", ErrInvalid, address)
		}
		network = "unix"
	case strings.HasPrefix(target, "tcp:127.0.0.1:"):
		port, err := strconv.Atoi(strings.TrimPrefix(target, "tcp:127.0.0.1:"))
		if err != nil || port < 1 || port > 65535 {
			return nil, nil, nil, fmt.Errorf("%w: tcp:127.0.0.1:<port>", ErrInvalid)
		}
		if !l.Allowed(port) {
			return nil, nil, nil, fmt.Errorf("%w: port %d is not registered for this launch", ErrForbidden, port)
		}
		network, address = "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	default:
		return nil, nil, nil, fmt.Errorf("%w: a target is unix:<name> or tcp:127.0.0.1:<port>", ErrInvalid)
	}
	select {
	case d.slots <- struct{}{}:
	default:
		return nil, nil, nil, fmt.Errorf("%w: %d streams are open", ErrBusy, config.MaxDials)
	}
	nc, err := net.DialTimeout(network, address, config.DialTimeout)
	if err != nil {
		<-d.slots
		return nil, nil, nil, fmt.Errorf("dialling %s: %w", target, err)
	}
	unlaunch, err := l.AddStream(func() { nc.Close() })
	if err != nil {
		nc.Close()
		<-d.slots
		if errors.Is(err, hooks.ErrNoLaunch) {
			return nil, nil, nil, ErrStaleLaunch
		}
		return nil, nil, nil, fmt.Errorf("%w: %v", ErrBusy, err)
	}
	var once sync.Once
	release := func() {
		once.Do(func() {
			unlaunch()
			<-d.slots
		})
	}
	return nc, l, release, nil
}

// linkedSocket is the socket a link in the launch's dial directory names, resolved: the path that
// is dialled, so the link cannot be swapped between the check and the dial. A link that names
// nothing yet is "not found", as a socket not made yet is: the program may still be binding it.
func (d *Dialer) linkedSocket(l *hooks.Launch, name, address string) (string, error) {
	real, err := filepath.EvalSymlinks(address)
	if err != nil {
		return "", fmt.Errorf("%w: %s links to no socket yet", ErrNotFound, name)
	}
	st, err := os.Lstat(real)
	if err != nil {
		return "", fmt.Errorf("%w: %s links to no socket yet", ErrNotFound, name)
	}
	if st.Mode()&fs.ModeSocket == 0 {
		return "", fmt.Errorf("%w: %s links to something that is not a socket", ErrForbidden, name)
	}
	// Another launch's sockets are that launch's: one member must not reach another's server
	// through the host.
	own := filepath.Dir(d.launches.RealDialPath(l, name))
	root := filepath.Dir(own)
	if strings.HasPrefix(real, root+string(filepath.Separator)) && !strings.HasPrefix(real, own+string(filepath.Separator)) {
		return "", fmt.Errorf("%w: %s links into another launch's dial directory", ErrForbidden, name)
	}
	if err := privateSocket(real, st); err != nil {
		return "", fmt.Errorf("%w: %s links to %v", ErrForbidden, name, err)
	}
	return real, nil
}
