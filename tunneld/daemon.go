package main

// The daemon: one provider, kept alive, and an honest status.
//
// Nothing is documented about how these clients behave after a laptop sleeps, and a process that is still
// running says nothing about whether it holds a connection. So the state comes from asking the client,
// every few seconds, and the address is proved by a request that goes out through it and comes back.
//
// A sleep shows up as a gap in the wall clock, a change of network as a change in this machine's
// addresses. Either marks the moment to look harder, and a client that has not recovered by itself soon
// after is started again. Not at once: a quick tunnel that is restarted comes back with a new address,
// and links already handed out die with the old one, so it gets its chance to reconnect first (cloudflared
// did, in what was measured: see desktop/README.md).

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// Timings, which the tests shorten.
type timings struct {
	tick       time.Duration // how often the client is asked
	sleepGap   time.Duration // a tick this late means the machine slept
	graceAfter time.Duration // how long a client may be down before it is started again
	settled    time.Duration // how long a start must last to reset the backoff
	maxBackoff time.Duration
	fatalWait  time.Duration // how long to wait before trying again what needs a person
	proofEvery time.Duration // first wait between proofs of the address, doubling
	proofTries int
}

func defaultTimings() timings {
	return timings{tick: 5 * time.Second, sleepGap: 20 * time.Second, graceAfter: 45 * time.Second, settled: time.Minute, maxBackoff: time.Minute, fatalWait: time.Minute, proofEvery: 2 * time.Second, proofTries: 8}
}

// backoff is the delay before the n-th start in a row that did not last.
func backoff(n int, most time.Duration) time.Duration {
	if n <= 1 {
		return time.Second
	}
	if n > 16 {
		return most
	}
	delay := time.Second << (n - 1)
	if delay > most {
		return most
	}
	return delay
}

// Daemon holds one tunnel.
type Daemon struct {
	provider Provider
	status   *statusFile
	router   *Router
	dir      string
	timing   timings
	logf     func(string, ...any)
	// addresses names this machine's network, and is a variable for the tests.
	addresses func() string
	// prove fetches a URL through the public address, and is a variable for the tests.
	prove func(ctx context.Context, url string) bool

	mu        sync.Mutex
	stopRun   context.CancelFunc
	children  map[int]bool
	listener  net.Listener
	downSince time.Time
	proving   context.CancelFunc
}

func randomPath() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "/.well-known/daedalus-" + fmt.Sprint(time.Now().UnixNano())
	}
	return "/.well-known/daedalus-" + hex.EncodeToString(b)
}

// NewDaemon wires a provider to a router and a status file.
func NewDaemon(provider Provider, config Config, dir string, logf func(string, ...any)) *Daemon {
	return &Daemon{
		provider:  provider,
		status:    newStatusFile(dir, config.Provider, logf),
		router:    NewRouter(config.DoorPort, config.ServicesPort, randomPath(), provider.Visitor()),
		dir:       dir,
		timing:    defaultTimings(),
		logf:      logf,
		addresses: machineAddresses,
		prove:     proveAddress,
		children:  map[int]bool{},
	}
}

var proofClient = &http.Client{
	Timeout: 10 * time.Second,
	// Refuse to follow: an address that redirects is not answering as this router would.
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	Transport:     &http.Transport{Proxy: http.ProxyFromEnvironment, DisableKeepAlives: true},
}

func proveAddress(ctx context.Context, url string) bool {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	// A vendor's page for browsers must not stand between this and the router.
	request.Header.Set("User-Agent", "daedalus-tunneld")
	request.Header.Set("ngrok-skip-browser-warning", "1")
	answer, err := proofClient.Do(request)
	if err != nil {
		return false
	}
	defer answer.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(answer.Body, 4096))
	return answer.StatusCode == http.StatusNoContent
}

// machineAddresses is every address of every interface that is up, sorted. Loopback is left out, and so
// are the link-local addresses that come and go by themselves.
func machineAddresses() string {
	interfaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	var found []string
	for _, item := range interfaces {
		if item.Flags&net.FlagUp == 0 || item.Flags&net.FlagLoopback != 0 {
			continue
		}
		addresses, err := item.Addrs()
		if err != nil {
			continue
		}
		for _, address := range addresses {
			prefix, ok := address.(*net.IPNet)
			if ok && !prefix.IP.IsLinkLocalUnicast() {
				found = append(found, prefix.IP.String())
			}
		}
	}
	sort.Strings(found)
	return strings.Join(found, ",")
}

// Run holds the tunnel until ctx ends, and leaves the status saying so.
func (d *Daemon) Run(ctx context.Context) error {
	router := ""
	if d.provider.NeedsRouterPort() {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return err
		}
		d.listener = listener
		server := d.router.Server(nil)
		go func() { _ = server.Serve(listener) }()
		defer server.Close()
		router = "http://" + listener.Addr().String()
	}
	go d.watch(ctx)
	starts := 0
	for ctx.Err() == nil {
		runContext, stop := context.WithCancel(ctx)
		d.mu.Lock()
		d.stopRun = stop
		d.mu.Unlock()
		started := time.Now()
		err := d.provider.Run(runContext, d.tunnel(router))
		stop()
		d.endProof()
		d.dropChildren()
		if ctx.Err() != nil {
			break
		}
		if time.Since(started) > d.timing.settled {
			starts = 0
		}
		starts++
		failure := failureOf(orLost(err))
		wait := backoff(starts, d.timing.maxBackoff)
		if failure.Fatal {
			wait = d.timing.fatalWait
			d.status.update(func(s *Status) {
				s.State, s.Code, s.Detail, s.Confirmed, s.SignIn = StateFailed, failure.Code, failure.Detail, false, ""
			})
		} else {
			d.status.update(func(s *Status) { s.State, s.Detail, s.Confirmed = StateReconnecting, failure.Detail, false })
		}
		d.logf("the tunnel stopped (%s); trying again in %s", redactLine(failure.Detail), wait)
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return nil
}

func orLost(err error) error {
	if err == nil || errors.Is(err, context.Canceled) {
		return errors.New("the connection was lost")
	}
	return err
}

func (d *Daemon) tunnel(router string) *Tunnel {
	return &Tunnel{
		Router: router,
		Dir:    d.dir,
		Logf:   d.logf,
		Serve: func(listener net.Listener) error {
			return d.router.Server(d.connContext()).Serve(listener)
		},
		Up: func(address string) {
			d.status.update(func(s *Status) {
				s.State, s.Address, s.SignIn, s.Code, s.Detail, s.Confirmed = StateUp, strings.TrimRight(address, "/"), "", "", "", false
			})
			d.startProof()
		},
		SignIn: func(url string) {
			d.status.update(func(s *Status) { s.State, s.SignIn = StateSignIn, url })
		},
		Child: func(pid int) {
			d.mu.Lock()
			d.children[pid] = true
			pids := d.pids()
			d.mu.Unlock()
			d.status.update(func(s *Status) { s.PIDs = pids })
		},
	}
}

func (d *Daemon) connContext() func(context.Context, net.Conn) context.Context {
	if _, ok := d.provider.(*tailscale); ok {
		return funnelContext
	}
	return nil
}

func (d *Daemon) pids() []int {
	pids := []int{os.Getpid()}
	for pid := range d.children {
		pids = append(pids, pid)
	}
	sort.Ints(pids)
	return pids
}

func (d *Daemon) dropChildren() {
	d.mu.Lock()
	d.children = map[int]bool{}
	pids := d.pids()
	d.mu.Unlock()
	d.status.update(func(s *Status) { s.PIDs = pids })
}

// startProof sends a request out through the public address and waits for it to come back to the router,
// with a few tries because a new name takes a moment to spread.
func (d *Daemon) startProof() {
	d.endProof()
	ctx, stop := context.WithCancel(context.Background())
	d.mu.Lock()
	d.proving = stop
	d.mu.Unlock()
	address := d.status.read().Address
	go func() {
		wait := d.timing.proofEvery
		for try := 0; try < d.timing.proofTries; try++ {
			if d.prove(ctx, address+d.router.Probe) {
				d.status.update(func(s *Status) {
					if s.State == StateUp && s.Address == address {
						s.Confirmed = true
					}
				})
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(wait):
			}
			wait *= 2
		}
		d.logf("the address did not answer through the tunnel yet")
	}()
}

func (d *Daemon) endProof() {
	d.mu.Lock()
	stop := d.proving
	d.proving = nil
	d.mu.Unlock()
	if stop != nil {
		stop()
	}
}

// watch asks the client how it is, notices a sleep or a new network, and restarts what did not recover.
func (d *Daemon) watch(ctx context.Context) {
	ticker := time.NewTicker(d.timing.tick)
	defer ticker.Stop()
	last := time.Now()
	network := d.addresses()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			// Round(0) drops the monotonic reading, which stands still while a machine sleeps.
			slept := now.Round(0).Sub(last.Round(0)) > d.timing.sleepGap
			last = now
			moved := false
			if current := d.addresses(); current != network {
				network, moved = current, true
			}
			if slept || moved {
				d.logf("the machine slept or changed network; looking at the tunnel again")
				// Measured: for some seconds after a thaw the address is dead and the last proof still
				// says otherwise, so what was proved before is not carried across.
				d.status.update(func(s *Status) { s.Wakes, s.Confirmed = s.Wakes+1, false })
			}
			d.look(ctx, slept || moved)
		}
	}
}

// look reads the client's own state into the status. Only a tunnel that has been up is judged: one that
// is starting, waiting for a sign-in or failed has its own state.
func (d *Daemon) look(ctx context.Context, jolted bool) {
	state := d.status.read().State
	if state != StateUp && state != StateReconnecting {
		return
	}
	asked, cancel := context.WithTimeout(ctx, d.timing.tick)
	defer cancel()
	connected := d.provider.Connected(asked)
	switch {
	case connected:
		d.mu.Lock()
		d.downSince = time.Time{}
		d.mu.Unlock()
		if state != StateUp || jolted {
			d.status.update(func(s *Status) { s.State, s.Detail = StateUp, "" })
			// Whether the address still answers is exactly what a sleep puts in doubt.
			d.startProof()
		}
	case ctx.Err() == nil:
		d.mu.Lock()
		if d.downSince.IsZero() {
			d.downSince = time.Now()
		}
		since, stop := d.downSince, d.stopRun
		d.mu.Unlock()
		d.status.update(func(s *Status) { s.State, s.Confirmed = StateReconnecting, false })
		if time.Since(since) > d.timing.graceAfter && stop != nil {
			d.logf("the client did not recover by itself; starting it again")
			d.mu.Lock()
			d.downSince = time.Time{}
			d.mu.Unlock()
			stop()
		}
	}
}
