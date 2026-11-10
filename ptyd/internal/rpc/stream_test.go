//go:build unix

package rpc_test

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

type streamed struct {
	Channel uint32 `json:"channel"`
	Pid     int    `json:"pid"`
	Path    string `json:"path"`
}

// streamFrames reads one exec.stream channel until its exit frame and its close: the tagged output
// by tag, and the exit as the daemon reported it.
func streamFrames(t *testing.T, frames <-chan wire.Frame, channel uint32) (map[byte]string, map[string]any) {
	t.Helper()
	out := map[byte]string{}
	var exit map[string]any
	deadline := time.After(15 * time.Second)
	for {
		select {
		case fr := <-frames:
			if fr.Channel != channel {
				continue
			}
			if len(fr.Payload) == 0 {
				if exit == nil {
					t.Fatalf("the channel closed without an exit frame; output %q", out)
				}
				return out, exit
			}
			if fr.Payload[0] == 'x' {
				if err := json.Unmarshal(fr.Payload[1:], &exit); err != nil {
					t.Fatalf("exit frame %q: %v", fr.Payload, err)
				}
				continue
			}
			out[fr.Payload[0]] += string(fr.Payload[1:])
		case <-deadline:
			t.Fatalf("no end of channel %d; output so far %q", channel, out)
		}
	}
}

// waitGone waits for a process to be gone, for the tests that prove nothing outlives a stream.
func waitGone(t *testing.T, pid int, what string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for syscall.Kill(pid, 0) == nil {
		if time.Now().After(deadline) {
			t.Fatalf("%s (pid %d) outlived its stream", what, pid)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// firstLine waits for the stream's first line of stdout: a pid the test program prints.
func firstLine(t *testing.T, frames <-chan wire.Frame, channel uint32) string {
	t.Helper()
	text := ""
	deadline := time.After(10 * time.Second)
	for !strings.Contains(text, "\n") {
		select {
		case fr := <-frames:
			if fr.Channel == channel && len(fr.Payload) > 0 && fr.Payload[0] == 'o' {
				text += string(fr.Payload[1:])
			}
		case <-deadline:
			t.Fatalf("no line on channel %d", channel)
		}
	}
	return strings.TrimSpace(strings.SplitN(text, "\n", 2)[0])
}

func TestExecStreamCarriesAProgramsStandardStreams(t *testing.T) {
	f, _ := startSide(t)
	var info struct {
		Capabilities struct {
			ExecStream int `json:"exec_stream"`
		} `json:"capabilities"`
	}
	f.call(t, "daemon.info", nil, &info)
	if info.Capabilities.ExecStream != 1 {
		t.Fatalf("exec_stream capability %d", info.Capabilities.ExecStream)
	}
	// The program list holds for streams as it does for exec.run.
	if we := f.callErr("exec.stream", map[string]any{"argv": []string{"sh", "-c", "cat"}}); we == nil || we.Code != wire.CodeForbidden {
		t.Fatalf("sh: %v", we)
	}
	if we := f.callErr("exec.stream", map[string]any{"argv": []string{"bash"}, "timeout_ms": 5}); we == nil || we.Code != wire.CodeInvalidParams {
		t.Fatalf("an unknown field: %v", we)
	}
	var s streamed
	f.call(t, "exec.stream", map[string]any{"argv": []string{"bash", "-c", `read line; echo "out:$line:$KEY"; echo "err:$line" >&2; exit 3`},
		"env": map[string]string{"KEY": "v1"}}, &s)
	if s.Channel == 0 || s.Pid <= 0 || !strings.HasSuffix(s.Path, "bash") {
		t.Fatalf("%+v", s)
	}
	if err := f.client.Send(s.Channel, []byte("hello\n")); err != nil {
		t.Fatal(err)
	}
	out, exit := streamFrames(t, f.client.Frames, s.Channel)
	if out['o'] != "out:hello:v1\n" || out['e'] != "err:hello\n" {
		t.Fatalf("output %q", out)
	}
	if exit["exit_code"] != float64(3) {
		t.Fatalf("exit %v", exit)
	}
}

// Closing the channel is how the host stops a server: the program, and what it started, end.
func TestClosingAnExecStreamEndsItsWholeGroup(t *testing.T) {
	f, _ := startSide(t)
	var s streamed
	f.call(t, "exec.stream", map[string]any{"argv": []string{"bash", "-c", `sleep 60 >/dev/null 2>&1 & echo $!; exec cat`}}, &s)
	child, err := strconv.Atoi(firstLine(t, f.client.Frames, s.Channel))
	if err != nil {
		t.Fatal(err)
	}
	if err := f.client.Send(s.Channel, nil); err != nil {
		t.Fatal(err)
	}
	waitGone(t, s.Pid, "the program")
	waitGone(t, child, "its background child")
}

// A program that ignores the end of its input and the hangup is killed after the graces.
func TestAnExecStreamThatIgnoresItsEndIsKilled(t *testing.T) {
	f, _ := startSide(t)
	var s streamed
	f.call(t, "exec.stream", map[string]any{"argv": []string{"bash", "-c", `trap '' HUP TERM; echo up; while :; do sleep 0.1; done`}}, &s)
	firstLine(t, f.client.Frames, s.Channel)
	start := time.Now()
	if err := f.client.Send(s.Channel, nil); err != nil {
		t.Fatal(err)
	}
	waitGone(t, s.Pid, "the program")
	if took := time.Since(start); took > 8*time.Second {
		t.Fatalf("ending took %s", took)
	}
}

// The host's connection ending (a restart, a crash) ends every program it streamed.
func TestAnExecStreamEndsWithItsConnection(t *testing.T) {
	f, side := startSide(t)
	other, err := clienttest.Dial(f.daemon.Config.RunDir)
	if err != nil {
		t.Fatal(err)
	}
	var s streamed
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := other.Call(ctx, "exec.stream", map[string]any{"argv": []string{"bash", "-c", `sleep 60 >/dev/null 2>&1 & echo $!; exec cat`}}, &s); err != nil {
		t.Fatal(err)
	}
	child, err := strconv.Atoi(firstLine(t, other.Frames, s.Channel))
	if err != nil {
		t.Fatal(err)
	}
	other.Close()
	waitGone(t, s.Pid, "the program")
	waitGone(t, child, "its background child")
	deadline := time.Now().Add(5 * time.Second)
	for side.Exec.Streams() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("%d streams still counted", side.Exec.Streams())
		}
		time.Sleep(20 * time.Millisecond)
	}
}
