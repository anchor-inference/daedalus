package main

// Running a client that is somebody else's program: ngrok, cloudflared, frpc.

import (
	"bufio"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// stopGrace is how long a client is given to end by itself once told.
const stopGrace = 5 * time.Second

// runClient starts a client and waits for it. Each line of its output goes to read, once, and never to
// a log as it stands: a client's lines are its own, and a redacted few are kept only on its failure.
func runClient(ctx context.Context, t *Tunnel, binary string, args, env []string, read func(string)) error {
	if binary == "" {
		return fatal(CodeNoClient, "no client program")
	}
	cmd := exec.Command(binary, args...)
	cmd.Env = env
	cmd.Dir = t.Dir
	output, input := io.Pipe()
	cmd.Stdout, cmd.Stderr = input, input
	// Something the client started may hold its output open after it has gone.
	cmd.WaitDelay = stopGrace
	clientAttributes(cmd)
	if err := cmd.Start(); err != nil {
		// Windows looks a path up with each of its program extensions and, finding none, says
		// exec.ErrNotFound rather than ErrNotExist: without the second test a missing client there
		// was retried for ever instead of waiting for a person.
		if errors.Is(err, os.ErrNotExist) || errors.Is(err, exec.ErrNotFound) {
			return fatal(CodeNoClient, "%s is not there", binary)
		}
		// Refused by the system: on Windows this is Defender holding the file.
		if errors.Is(err, os.ErrPermission) || strings.Contains(strings.ToLower(err.Error()), "virus") {
			return fatal(CodeBlocked, "the system would not run %s: %v", binary, err)
		}
		return err
	}
	afterStart(cmd)
	t.Child(cmd.Process.Pid)
	var lines sync.WaitGroup
	lines.Add(1)
	var tail []string
	go func() {
		defer lines.Done()
		scanner := bufio.NewScanner(output)
		scanner.Buffer(make([]byte, 64<<10), 1<<20)
		for scanner.Scan() {
			line := scanner.Text()
			if read != nil {
				read(line)
			}
			if tail = append(tail, line); len(tail) > 3 {
				tail = tail[1:]
			}
		}
	}()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	var err error
	select {
	case err = <-done:
	case <-ctx.Done():
		endClient(cmd)
		select {
		case err = <-done:
		case <-time.After(stopGrace):
			_ = cmd.Process.Kill()
			err = <-done
		}
	}
	input.Close()
	lines.Wait()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	for _, line := range tail {
		t.Logf("%s: %s", binaryName(binary), redactLine(line))
	}
	if err == nil {
		return errors.New("the client ended by itself")
	}
	return err
}

func binaryName(path string) string {
	name := path
	if i := strings.LastIndexAny(name, `/\`); i >= 0 {
		name = name[i+1:]
	}
	return strings.TrimSuffix(name, ".exe")
}

// clientEnv is a client's whole environment: a client needs a home of its own, so an operator's
// ~/.cloudflared or ~/.config/ngrok cannot change what this tunnel is, and nothing of the launcher's.
func clientEnv(home string) []string {
	env := []string{"HOME=" + home, "USERPROFILE=" + home, "XDG_CONFIG_HOME=" + home}
	for _, key := range []string{"PATH", "LANG", "SYSTEMROOT", "TEMP", "TMP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} {
		if value := os.Getenv(key); value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}
