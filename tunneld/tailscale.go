package main

// Tailscale Funnel through tsnet: a Tailscale node inside this program, under the operator's own
// account, so there is no client to install and the operator's own Tailscale, if they have one, is left
// alone. Tailscale publishes tsnet to be embedded ("a library that lets you embed Tailscale inside a Go
// program", BSD-3-Clause), which is what sets it apart from the other vendors' clients here.
//
// Nothing is decrypted on the way: the certificate is this node's, so TLS ends in this process. The
// visitor comes from the Funnel connection itself and not from a header, and no port is opened on this
// machine at all, because connections are handed straight to the router.

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/ipn"
	"tailscale.com/tsnet"
)

const tailnetHostname = "daedalus"

type tailscale struct {
	dir string

	mu     sync.Mutex
	client *local.Client
}

func (ts *tailscale) NeedsRouterPort() bool { return false }

func (ts *tailscale) Visitor() VisitorOf {
	return func(r *http.Request) string {
		visitor, _ := r.Context().Value(funnelVisitorKey{}).(string)
		return visitor
	}
}

type funnelVisitorKey struct{}

// funnelContext notes the visitor on the connection, which is the only place the Funnel states it.
func funnelContext(ctx context.Context, conn net.Conn) context.Context {
	if secured, ok := conn.(*tls.Conn); ok {
		conn = secured.NetConn()
	}
	if funnel, ok := conn.(*ipn.FunnelConn); ok {
		return context.WithValue(ctx, funnelVisitorKey{}, funnel.Src.Addr().Unmap().String())
	}
	return ctx
}

func funnelFailure(err error) error {
	text := err.Error()
	switch {
	case strings.Contains(text, "HTTPS must be enabled"), strings.Contains(text, "HTTPS"):
		return fatal(CodeHTTPSOff, "%s", text)
	case strings.Contains(text, "funnel"), strings.Contains(text, "Funnel"):
		return fatal(CodeFunnelOff, "%s", text)
	}
	return err
}

func (ts *tailscale) Run(ctx context.Context, t *Tunnel) error {
	server := &tsnet.Server{
		Dir:      filepath.Join(ts.dir, "tailscale"),
		Hostname: tailnetHostname,
		UserLogf: func(string, ...any) {},
	}
	defer server.Close()
	if err := server.Start(); err != nil {
		return err
	}
	client, err := server.LocalClient()
	if err != nil {
		return err
	}
	if err := ts.waitForSignIn(ctx, client, t); err != nil {
		return err
	}
	ts.mu.Lock()
	ts.client = client
	ts.mu.Unlock()
	defer func() {
		ts.mu.Lock()
		ts.client = nil
		ts.mu.Unlock()
	}()
	listener, err := server.ListenFunnel("tcp", ":443", tsnet.FunnelOnly())
	if err != nil {
		return funnelFailure(err)
	}
	defer listener.Close()
	domains := server.CertDomains()
	if len(domains) == 0 {
		return fatal(CodeHTTPSOff, "this tailnet gave the node no name to serve")
	}
	t.Up("https://" + domains[0])
	serving := make(chan error, 1)
	go func() { serving <- t.Serve(listener) }()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-serving:
		return err
	}
}

// waitForSignIn asks the node until it is signed in, handing the operator the page when one is needed.
func (ts *tailscale) waitForSignIn(ctx context.Context, client *local.Client, t *Tunnel) error {
	for {
		status, err := client.StatusWithoutPeers(ctx)
		if err != nil && ctx.Err() != nil {
			return ctx.Err()
		}
		if err == nil {
			if status.BackendState == ipn.Running.String() {
				return nil
			}
			if status.AuthURL != "" {
				t.SignIn(status.AuthURL)
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

func (ts *tailscale) Connected(ctx context.Context) bool {
	ts.mu.Lock()
	client := ts.client
	ts.mu.Unlock()
	if client == nil {
		return false
	}
	status, err := client.StatusWithoutPeers(ctx)
	if err != nil {
		return false
	}
	// Not Self.Online: nothing promises a node hears that about itself, and a tunnel called down on a
	// field that stays unset would be restarted for nothing. Whether the address answers is proved
	// from outside after any sleep, whatever this says.
	return status.BackendState == ipn.Running.String()
}
