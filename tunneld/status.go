package main

// What the launcher reads. A file and not a socket: the launcher may read it from a request handler,
// from a start that has not reached the daemon yet, and after the daemon has died, and each of those
// has a true answer in the last thing written. Renamed into place, so a reader never sees half of it.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// The states a tunnel reports. "up" means its client holds a connection; Confirmed says that a request
// to the public address has also come back through it, which is the only proof the address answers.
const (
	StateStarting     = "starting"
	StateSignIn       = "sign_in"
	StateUp           = "up"
	StateReconnecting = "reconnecting"
	StateFailed       = "failed"
)

// Failure codes, which the app words in the operator's language. Detail stays in English for the log.
const (
	CodeNoClient      = "no_client"
	CodeTokenRejected = "token_rejected"
	CodeBlocked       = "blocked"
	CodeHTTPSOff      = "https_off"
	CodeFunnelOff     = "funnel_off"
	CodeServerRefused = "server_refused"
	CodeNotConfigured = "not_configured"
	CodeOther         = "other"
)

// Status is the whole of status.json.
type Status struct {
	Provider  string    `json:"provider"`
	State     string    `json:"state"`
	Address   string    `json:"address,omitempty"`
	SignIn    string    `json:"sign_in,omitempty"`
	Code      string    `json:"code,omitempty"`
	Detail    string    `json:"detail,omitempty"`
	Confirmed bool      `json:"confirmed"`
	Since     time.Time `json:"since"`
	Updated   time.Time `json:"updated"`
	// PIDs are this process and any client it runs. The host keeps them out of its tunnel watch: they
	// come by way of the launcher, so nothing the agent raises can name itself the app's own.
	PIDs []int `json:"pids"`
	// Ports are the loopback ports a client opened for itself, which the host seals from the agent.
	Ports []int `json:"ports,omitempty"`
	Wakes int   `json:"wakes"`
	// VisitorUnknown says the provider did not name a visitor: everyone through it then counts as one
	// address to every limit.
	VisitorUnknown bool `json:"visitor_unknown,omitempty"`
}

type statusFile struct {
	mu      sync.Mutex
	path    string
	current Status
	logf    func(string, ...any)
}

func newStatusFile(dir, provider string, logf func(string, ...any)) *statusFile {
	now := time.Now().UTC()
	return &statusFile{path: filepath.Join(dir, "status.json"), logf: logf, current: Status{Provider: provider, State: StateStarting, Since: now, PIDs: []int{os.Getpid()}}}
}

func (s *statusFile) update(change func(*Status)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	before := s.current.State
	change(&s.current)
	now := time.Now().UTC()
	if s.current.State != before {
		s.current.Since = now
		s.logf("the tunnel is %s", s.current.State)
	}
	s.current.Updated = now
	s.write()
}

func (s *statusFile) read() Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.current
}

func (s *statusFile) write() {
	body, err := json.Marshal(s.current)
	if err != nil {
		return
	}
	scratch := s.path + ".tmp"
	if err := os.WriteFile(scratch, body, 0o600); err != nil {
		s.logf("the status file: %v", err)
		return
	}
	if err := replaceFile(scratch, s.path); err != nil {
		s.logf("the status file: %v", err)
	}
}

// replaceFile renames from over to, waiting out a reader that holds to open. On Windows a reader
// that opened the file without sharing its deletion (os.ReadFile does not) makes the rename fail
// with "Access is denied" until it closes it, and the launcher reads this file while tunneld writes
// it: a state lost that way stayed lost until the next change, which for a failure needing a
// person may never come. A read takes microseconds; a second is far more than one needs.
func replaceFile(from, to string) error {
	deadline := time.Now().Add(time.Second)
	for {
		err := os.Rename(from, to)
		if err == nil || !heldByReader(err) || time.Now().After(deadline) {
			return err
		}
		time.Sleep(5 * time.Millisecond)
	}
}
