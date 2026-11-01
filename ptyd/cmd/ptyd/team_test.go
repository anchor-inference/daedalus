//go:build unix

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/server/clienttest"
)

// subcommand runs the test binary as `ptyd <args>` with a launch's environment.
func subcommand(env map[string]string, args ...string) *exec.Cmd {
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), runMain+"=1", "PTYD_TEST_ARGS="+strings.Join(args, " "))
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	return cmd
}

// TestTeamToolsAgainstARunningDaemon is the bridge end to end: a daemon, a launch registered over
// its socket, `ptyd team-mcp` driven the way a CLI drives it, the host's replies sent with
// hooks.reply, and `ptyd hook` posting a command hook for the same launch.
func TestTeamToolsAgainstARunningDaemon(t *testing.T) {
	base, err := os.MkdirTemp("", "ptyd")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(base)
	run, state := filepath.Join(base, "run"), filepath.Join(base, "state")
	daemon(t, "serve", "--env", "container", "--run-dir", run, "--state-dir", state, "--home", base)
	waitFile(t, filepath.Join(run, server.EndpointFile))
	c, err := clienttest.Dial(run)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	call := func(method string, params, result any) {
		t.Helper()
		if err := c.Call(ctx, method, params, result); err != nil {
			t.Fatalf("%s: %v", method, err)
		}
	}
	call("events.subscribe", map[string]any{"after_seq": 0}, nil)
	var launch struct {
		Env map[string]string `json:"env"`
	}
	call("hooks.register_launch", map[string]any{"launch_id": "team1", "terminal_id": "w1", "hold_max_ms": 60000}, &launch)
	if launch.Env["DAEDALUS_PTYD_BIN"] == "" {
		t.Fatalf("the launch does not name the daemon's binary: %v", launch.Env)
	}
	env := map[string]string{"DAEDALUS_HOOK_URL": launch.Env["DAEDALUS_HOOK_URL"], "DAEDALUS_HOOK_TOKEN": launch.Env["DAEDALUS_HOOK_TOKEN"],
		"DAEDALUS_ASK_HOLD_MS": "30000", "DAEDALUS_LAUNCH_ID": "team1"}

	mcp := subcommand(env, "team-mcp")
	stdin, _ := mcp.StdinPipe()
	stdout, _ := mcp.StdoutPipe()
	if err := mcp.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = mcp.Process.Kill()
		_ = mcp.Wait()
	}()
	lines := bufio.NewScanner(stdout)
	send := func(id int, method string, params any) {
		t.Helper()
		b, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
		if _, err := stdin.Write(append(b, '\n')); err != nil {
			t.Fatal(err)
		}
	}
	responses := make(chan map[string]any, 16)
	go func() {
		for lines.Scan() {
			var m map[string]any
			_ = json.Unmarshal(lines.Bytes(), &m)
			responses <- m
		}
		close(responses)
	}()
	next := func() map[string]any {
		t.Helper()
		select {
		case m, ok := <-responses:
			if !ok {
				t.Fatal("team-mcp closed its output")
			}
			return m
		case <-time.After(20 * time.Second):
			t.Fatal("no response from team-mcp")
		}
		return nil
	}
	text := func(m map[string]any) string {
		t.Helper()
		r, _ := m["result"].(map[string]any)
		content, _ := r["content"].([]any)
		if len(content) != 1 || r["isError"] != false {
			t.Fatalf("%v", m)
		}
		return content[0].(map[string]any)["text"].(string)
	}
	// The server announces that the tools were loaded; those posts are counted apart from the calls.
	var hellos []string
	hookEvent := func() (body map[string]any, replyID string) {
		t.Helper()
		for {
			ev, err := c.WaitEvent("hook", "w1", 20*time.Second)
			if err != nil {
				t.Fatal(err)
			}
			var data struct {
				Name    string         `json:"name"`
				Body    map[string]any `json:"body"`
				ReplyID string         `json:"reply_id"`
			}
			_ = json.Unmarshal(ev.Data, &data)
			if data.Name != "team" && data.Name != "Stop" {
				t.Fatalf("hook %q", data.Name)
			}
			if data.Body["tool"] == "hello" {
				hellos = append(hellos, fmt.Sprint(data.Body["stage"]))
				continue
			}
			return data.Body, data.ReplyID
		}
	}

	send(1, "initialize", map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{}, "clientInfo": map[string]any{"name": "test"}})
	if v := next()["result"].(map[string]any)["protocolVersion"]; v != "2025-06-18" {
		t.Fatalf("protocol %v", v)
	}
	_, _ = stdin.Write([]byte(`{"jsonrpc":"2.0","method":"notifications/initialized"}` + "\n"))
	send(2, "tools/list", nil)
	if tools := next()["result"].(map[string]any)["tools"].([]any); len(tools) != 5 {
		t.Fatalf("%v", tools)
	}

	send(3, "tools/call", map[string]any{"name": "Report", "arguments": map[string]any{"kind": "checkpoint", "note": "tests pass"}})
	body, replyID := hookEvent()
	if body["tool"] != "report" || body["note"] != "tests pass" || replyID == "" || !strings.HasPrefix(fmt.Sprint(body["call_id"]), "team1:") {
		t.Fatalf("%v %q", body, replyID)
	}
	// The announcements are posted beside the calls, not before them, so they may still be on their
	// way once the report has arrived; nothing else is in flight here, so every event now is one.
	for len(hellos) < 2 {
		ev, err := c.WaitEvent("hook", "w1", 5*time.Second)
		if err != nil {
			break
		}
		var data struct {
			Body map[string]any `json:"body"`
		}
		_ = json.Unmarshal(ev.Data, &data)
		if data.Body["tool"] != "hello" {
			t.Fatalf("an unexpected hook while waiting for the announcements: %v", data.Body)
		}
		hellos = append(hellos, fmt.Sprint(data.Body["stage"]))
	}
	sort.Strings(hellos)
	if fmt.Sprint(hellos) != "[initialize tools/list]" {
		t.Fatalf("the loading of the tools was announced as %v", hellos)
	}
	call("hooks.reply", map[string]any{"launch_id": "team1", "reply_id": replyID, "body": map[string]any{"text": "recorded"}}, nil)
	if got := text(next()); got != "recorded" {
		t.Fatalf("report: %q", got)
	}

	send(4, "tools/call", map[string]any{"name": "AskOrchestrator", "arguments": map[string]any{"question": "Which branch?", "options": []string{"main", "dev"}}})
	body, replyID = hookEvent()
	if body["tool"] != "ask" || body["question"] != "Which branch?" {
		t.Fatalf("%v", body)
	}
	call("hooks.reply", map[string]any{"reply_id": replyID, "body": map[string]any{"text": "dev"}}, nil)
	if got := text(next()); got != "dev" {
		t.Fatalf("ask: %q", got)
	}

	// The command hook of the same launch, and after the launch is gone: still exit 0, where
	// hook-post says 2.
	hook := subcommand(env, "hook", "Stop")
	hook.Stdin = strings.NewReader(`{"hook_event_name":"Stop"}`)
	if out, err := hook.Output(); err != nil || len(out) != 0 {
		t.Fatalf("hook: %v %q", err, out)
	}
	if body, _ := hookEvent(); body["hook_event_name"] != "Stop" {
		t.Fatalf("%v", body)
	}
	call("hooks.unregister_launch", map[string]any{"launch_id": "team1"}, nil)
	hook = subcommand(env, "hook", "Stop")
	hook.Stdin = strings.NewReader(`{}`)
	if out, err := hook.CombinedOutput(); err != nil || !strings.Contains(string(out), "410") {
		t.Fatalf("hook after the launch: %v %q", err, out)
	}
	post := subcommand(env, "hook-post", "Stop")
	post.Stdin = strings.NewReader(`{}`)
	if err := post.Run(); err == nil {
		t.Fatal("hook-post after the launch succeeded")
	}

	// Closing its input ends the server.
	stdin.Close()
	done := make(chan error, 1)
	go func() { done <- mcp.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("team-mcp exit: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("team-mcp did not end with its input")
	}
}
