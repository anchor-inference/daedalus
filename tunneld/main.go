// tunneld holds one public address for a native Daedalus: a tunnel by the provider the operator chose,
// and the router behind it, which shows agent links and shared services and nothing else.
//
// It is a program of its own so that what a virus scanner, or a crash, or a vendor's client does to it
// costs only the public address. The launcher starts it when the operator turns the address on, and it
// ends when the launcher does: the pipe on its stdin closes however the launcher went, so an address
// never goes on answering for an app that is closed.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
)

// version is stamped at build time.
var version = "dev"

func main() {
	log.SetFlags(log.LstdFlags)
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: tunneld serve <dir> | tunneld version")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "version":
		fmt.Println(version)
	case "serve":
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, "usage: tunneld serve <dir>")
			os.Exit(2)
		}
		if err := serve(os.Args[2]); err != nil {
			log.Printf("tunneld: %v", err)
			os.Exit(1)
		}
	default:
		fmt.Fprintf(os.Stderr, "tunneld: no such command: %s\n", os.Args[1])
		os.Exit(2)
	}
}

func readJSON(path string, into any) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(body, into)
}

func serve(dir string) error {
	// Tailscale's client uploads its logs by default. This node serves one router and nobody asked for that.
	_ = os.Setenv("TS_NO_LOGS_NO_SUPPORT", "true")
	var config Config
	if err := readJSON(filepath.Join(dir, "config.json"), &config); err != nil {
		return fmt.Errorf("the configuration: %w", err)
	}
	var secrets Secrets
	if err := readJSON(filepath.Join(dir, "secrets.json"), &secrets); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("the secrets file: %w", err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	// The launcher holds the other end of stdin. A read that ends means the launcher is gone.
	go func() {
		_, _ = io.Copy(io.Discard, os.Stdin)
		log.Printf("the launcher is gone; closing the address")
		stop()
	}()
	provider, err := newProvider(config, secrets, dir)
	if err != nil {
		status := newStatusFile(dir, config.Provider, log.Printf)
		failure := failureOf(err)
		status.update(func(s *Status) { s.State, s.Code, s.Detail = StateFailed, failure.Code, failure.Detail })
		<-ctx.Done()
		return nil
	}
	return NewDaemon(provider, config, dir, log.Printf).Run(ctx)
}
