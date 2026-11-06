package main

// A provider is one way of getting a public address. Adding one is writing this interface and a line
// in newProvider; nothing else here, in the launcher or in the host knows how any of them works.

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"time"
)

// Provider holds one public address for as long as Run runs.
type Provider interface {
	// Visitor is how this provider states who connected.
	Visitor() VisitorOf
	// NeedsRouterPort is true of a client that dials, and so needs the router on a loopback port. One
	// that hands connections over serves the router itself and leaves no port open at all.
	NeedsRouterPort() bool
	// Run opens the tunnel, reports through t, and returns when ctx ends or the tunnel is lost.
	Run(ctx context.Context, t *Tunnel) error
	// Connected says whether the client holds a connection this moment, which is what tells the truth
	// after a sleep: a process that is running proves nothing.
	Connected(ctx context.Context) bool
}

// Tunnel is what a provider is given to work with, and how it reports.
type Tunnel struct {
	// Router is http://127.0.0.1:<port>, for a client that dials.
	Router string
	// Serve hands a listener to the router, for a client that hands over connections.
	Serve func(net.Listener) error
	// Up says what the public address is. Called again if it changes.
	Up func(address string)
	// SignIn asks the operator to open a page.
	SignIn func(url string)
	// Child records a client process, so it can be told from a stranger's.
	Child func(pid int)
	// Dir is this daemon's private folder.
	Dir string
	// Logf logs. Nothing passed here may hold a path, a token or a whole line of a client's output.
	Logf func(string, ...any)
}

// Failure is a tunnel that will not come up by trying again now. Fatal ones wait for a person.
type Failure struct {
	Code   string
	Detail string
	Fatal  bool
}

func (f *Failure) Error() string { return f.Code + ": " + f.Detail }

func fatal(code, format string, args ...any) *Failure {
	return &Failure{Code: code, Detail: fmt.Sprintf(format, args...), Fatal: true}
}

func failureOf(err error) *Failure {
	var failure *Failure
	if errors.As(err, &failure) {
		return failure
	}
	return &Failure{Code: CodeOther, Detail: err.Error()}
}

// Config is config.json, which the launcher writes. Secrets are in another file, and the only fields
// here that name a place outside this daemon are the program to run and the operator's own server.
type Config struct {
	Provider     string `json:"provider"`
	DoorPort     string `json:"door_port"`
	ServicesPort string `json:"services_port"`
	// Binary is the client program for a provider that has one.
	Binary string `json:"binary,omitempty"`
	// FrpServer is host:port of the operator's frps, and FrpDomain the name it serves.
	FrpServer string `json:"frp_server,omitempty"`
	FrpDomain string `json:"frp_domain,omitempty"`
}

// Secrets is secrets.json, 0600, in a folder the agent's policy seals whole. Read from a file and never
// from an argument or the environment, which any process of this user can list.
type Secrets struct {
	NgrokToken string `json:"ngrok_token,omitempty"`
	FrpToken   string `json:"frp_token,omitempty"`
}

func newProvider(config Config, secrets Secrets, dir string) (Provider, error) {
	switch config.Provider {
	case "ngrok":
		return &ngrok{binary: config.Binary, token: secrets.NgrokToken, dir: dir}, nil
	case "tailscale":
		return &tailscale{dir: dir}, nil
	case "cloudflare":
		return &cloudflare{binary: config.Binary, dir: dir}, nil
	case "frp":
		return &frp{binary: config.Binary, server: config.FrpServer, domain: config.FrpDomain, token: secrets.FrpToken, dir: dir}, nil
	}
	return nil, fatal(CodeNotConfigured, "no provider named %q", config.Provider)
}

// freeLoopbackPort asks the system for a port a client's local API can sit on. The small race with a
// program that takes it first ends in the client failing to start, and the next start asks again.
func freeLoopbackPort() (string, error) {
	held, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	defer held.Close()
	return strconv.Itoa(held.Addr().(*net.TCPAddr).Port), nil
}

var localAPI = &http.Client{Timeout: 3 * time.Second, Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}}

// redactions keep a client's output safe to keep. Paths hold invites, and nothing else in a line of
// theirs is worth a secret in a log that the operator may paste into an issue.
var redactions = []*regexp.Regexp{
	regexp.MustCompile(`/[as]/[^\s"']+`),
	regexp.MustCompile(`[A-Za-z0-9_=-]{32,}`),
}

func redactLine(line string) string {
	for _, pattern := range redactions {
		line = pattern.ReplaceAllString(line, "…")
	}
	return line
}
