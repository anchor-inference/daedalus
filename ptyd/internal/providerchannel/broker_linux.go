//go:build linux

package providerchannel

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
)

const maxConnections = 16

type dialContext func(context.Context, string, string) (net.Conn, error)

// Broker owns one launch's socket and all tunnels. Close closes the listener and every active
// stream before removing its private directory, so a held mount cannot keep granting access.
type Broker struct {
	directory string
	listener  *net.UnixListener
	target    PinnedProvider
	dial      dialContext
	ctx       context.Context
	cancel    context.CancelFunc

	mu         sync.Mutex
	active     map[net.Conn]struct{}
	closed     bool
	closeErr   error
	workers    sync.WaitGroup
	acceptDone chan struct{}
	closeDone  chan struct{}
}

func ownedByCurrentUser(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid())
}

// StartPinnedBroker creates an owned socket below a private daemon directory. The resolver has
// already run; only the selected numeric IP and port are passed to the TCP dialer.
func StartPinnedBroker(parent string, target PinnedProvider) (*Broker, error) {
	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	return startBroker(parent, target, dialer.DialContext)
}

func startBroker(parent string, target PinnedProvider, dial dialContext) (*Broker, error) {
	if !publicProviderAddress(target.address) || target.authority == "" || dial == nil {
		return nil, errors.New("provider channel: target was not pinned")
	}
	stat, err := os.Lstat(parent)
	if err != nil || !stat.IsDir() || stat.Mode().Perm()&0077 != 0 || !ownedByCurrentUser(stat) {
		return nil, errors.New("provider channel: parent directory must be private and owned by this user")
	}
	directory, err := os.MkdirTemp(parent, "provider-")
	if err != nil {
		return nil, err
	}
	socket := filepath.Join(directory, "provider.sock")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		_ = os.Remove(directory)
		return nil, err
	}
	if err := os.Chmod(socket, 0600); err != nil {
		listener.Close()
		_ = os.Remove(socket)
		_ = os.Remove(directory)
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	broker := &Broker{directory: directory, listener: listener, target: target, dial: dial,
		ctx: ctx, cancel: cancel, active: map[net.Conn]struct{}{}, acceptDone: make(chan struct{}), closeDone: make(chan struct{})}
	go broker.accept()
	return broker, nil
}

func (b *Broker) accept() {
	defer close(b.acceptDone)
	for {
		client, err := b.listener.AcceptUnix()
		if err != nil {
			return
		}
		b.mu.Lock()
		if b.closed || len(b.active) >= maxConnections {
			b.mu.Unlock()
			client.Close()
			continue
		}
		b.active[client] = struct{}{}
		b.workers.Add(1)
		b.mu.Unlock()
		go b.serve(client)
	}
}

func (b *Broker) serve(client *net.UnixConn) {
	defer b.workers.Done()
	defer b.remove(client)
	_ = client.SetReadDeadline(time.Now().Add(10 * time.Second))
	_ = sandbox.ServeProviderConnect(client, b.target.authority, func() (io.ReadWriteCloser, error) {
		_ = client.SetReadDeadline(time.Time{})
		address := net.JoinHostPort(b.target.address.String(), "443")
		upstream, err := b.dial(b.ctx, "tcp", address)
		if err != nil {
			return nil, err
		}
		b.mu.Lock()
		if b.closed {
			b.mu.Unlock()
			upstream.Close()
			return nil, errors.New("provider channel: broker closed before connection")
		}
		b.active[upstream] = struct{}{}
		b.mu.Unlock()
		return &trackedConnection{Conn: upstream, remove: b.remove}, nil
	})
}

type trackedConnection struct {
	net.Conn
	remove func(net.Conn)
	once   sync.Once
}

func (c *trackedConnection) Close() error {
	err := c.Conn.Close()
	c.once.Do(func() { c.remove(c.Conn) })
	return err
}

func (c *trackedConnection) CloseWrite() error {
	if half, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return half.CloseWrite()
	}
	return nil
}

func (b *Broker) remove(conn net.Conn) {
	b.mu.Lock()
	delete(b.active, conn)
	b.mu.Unlock()
}

// Directory returns a descriptor for the scoped mount. A pending launch must still fail closed
// if Close races it: the mount validation requires the socket entry to remain present.
func (b *Broker) Directory() (*os.File, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return nil, errors.New("provider channel: broker closed")
	}
	return os.Open(b.directory)
}

// Close revokes new connections and established streams and waits for their handlers to exit.
func (b *Broker) Close() error {
	b.mu.Lock()
	if b.closed {
		b.mu.Unlock()
		<-b.closeDone
		return b.closeErr
	}
	b.closed = true
	connections := make([]net.Conn, 0, len(b.active))
	for conn := range b.active {
		connections = append(connections, conn)
	}
	b.mu.Unlock()
	b.cancel()
	_ = b.listener.Close()
	for _, conn := range connections {
		_ = conn.Close()
	}
	<-b.acceptDone
	b.workers.Wait()
	if err := os.Remove(filepath.Join(b.directory, "provider.sock")); err != nil && !errors.Is(err, os.ErrNotExist) {
		b.closeErr = err
		close(b.closeDone)
		return err
	}
	err := os.Remove(b.directory)
	b.closeErr = err
	close(b.closeDone)
	return err
}
