package sessions

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// updateWire rewrites the wire samples instead of comparing with them:
//
//	go test ./internal/sidechan/sessions -run TestWireSamples -update-wire
//
// The samples are the exact JSON sessions.scan and sessions.read answer for a synthetic Claude Code
// and Codex session. The host's tests (tests/unit/test_foreign_wire.py) read the same files and
// import them, so a change of shape on this side fails here until the samples are rewritten, and
// then fails there if the host does not read the new shape.
var updateWire = flag.Bool("update-wire", false, "rewrite testdata/wire from the parsers' output")

// wireSample is one session as the host meets it: the scan of its folder and every page of its read
// joined, with the last page's bookkeeping.
type wireSample struct {
	Scan ScanReply `json:"scan"`
	Read struct {
		Header Header `json:"header"`
		Turns  []Turn `json:"turns"`
		Next   int64  `json:"next"`
		Done   bool   `json:"done"`
		Live   bool   `json:"live"`
		Masked int    `json:"masked"`
		Offset int64  `json:"offset"`
		Total  int    `json:"total"`
	} `json:"read"`
}

func sample(t *testing.T, env *Env, s *Service, harness, id string) []byte {
	t.Helper()
	var out wireSample
	scan, err := s.Scan(ScanRequest{Harness: harness, Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	out.Scan = scan
	turns, last := readAll(t, s, harness, id, true, MinPage)
	out.Read.Header, out.Read.Turns = last.Header, turns
	out.Read.Next, out.Read.Done, out.Read.Live, out.Read.Offset, out.Read.Total = last.Next, last.Done, last.Live, last.Offset, last.Total
	masked := 0
	for from := int64(0); ; {
		r, err := s.Read(ReadRequest{Harness: harness, ID: id, From: from, MaxBytes: MinPage})
		if err != nil {
			t.Fatal(err)
		}
		page := r.(ReadReply)
		masked += page.Masked
		if page.Done {
			break
		}
		from = page.Next
	}
	out.Read.Masked = masked
	b, err := json.MarshalIndent(out, "", " ")
	if err != nil {
		t.Fatal(err)
	}
	// The test's home is a temporary folder; the samples name the one the fixtures pretend to be.
	return append(bytes.ReplaceAll(b, []byte(env.Home), []byte("/home/someone")), '\n')
}

func claudeWireSession(t *testing.T, env *Env) {
	token := "ghp_" + strings.Repeat("Q1w2E3r4T5", 4)
	dir := filepath.Join(claudeStore(env), "-home-someone-proj")
	writeFile(t, filepath.Join(dir, "c0ffee00-0000-4000-8000-000000000001.jsonl"), jsonl(t,
		cUser("u1", "", "2026-03-01T10:00:00Z", "fix the flaky test"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "thinking", "thinking": "Look first.", "signature": "sig"}),
		cAssistant("a1b", "a1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "tool_use", "id": "toolu_bash", "name": "Bash",
			"input": map[string]any{"command": "pytest -q tests/test_door.py", "timeout": 120000}}),
		cUser("r1", "a1b", "2026-03-01T10:00:02Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_bash",
			"content": "1 failed; GITHUB_TOKEN=" + token, "is_error": true}}),
		cAssistant("a2", "r1", "2026-03-01T10:00:03Z", "msg_2", map[string]any{"type": "tool_use", "id": "toolu_todo", "name": "TodoWrite",
			"input": map[string]any{"todos": []any{map[string]any{"content": "fix", "status": "in_progress"}}}}),
		cUser("r2", "a2", "2026-03-01T10:00:04Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_todo", "content": "ok"}}),
		cAssistant("a3", "r2", "2026-03-01T10:00:05Z", "msg_3", map[string]any{"type": "tool_use", "id": "toolu_task", "name": "Task",
			"input": map[string]any{"description": "find callers", "prompt": "look"}}),
		cUser("r3", "a3", "2026-03-01T10:00:09Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_task", "content": "two callers"}}),
		cAssistant("a4", "r3", "2026-03-01T10:00:10Z", "msg_4", map[string]any{"type": "tool_use", "id": "toolu_mcp", "name": "mcp__notes__append",
			"input": map[string]any{"text": "door"}}),
		cUser("r4", "a4", "2026-03-01T10:00:11Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_mcp", "content": "appended"}}),
		claudeRec{"type": "system", "subtype": "compact_boundary", "uuid": "cb", "parentUuid": nil, "logicalParentUuid": "r4",
			"timestamp": "2026-03-01T10:00:12Z", "compactMetadata": map[string]any{"trigger": "auto"}},
		cUser("cs", "cb", "2026-03-01T10:00:12Z", "Summary: the door test was fixed", map[string]any{"isCompactSummary": true}),
		cUser("u2", "cs", "2026-03-01T10:00:13Z", []any{map[string]any{"type": "text", "text": "and this picture?"},
			map[string]any{"type": "image", "source": map[string]any{"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}}),
		cAssistant("a5", "u2", "2026-03-01T10:00:14Z", "msg_5", map[string]any{"type": "text", "text": "A door."}),
		cAssistant("a6", "a5", "2026-03-01T10:00:15Z", "msg_6", map[string]any{"type": "tool_use", "id": "toolu_dangling", "name": "Read",
			"input": map[string]any{"file_path": "/home/someone/proj/door.py"}}),
	))
	sub := filepath.Join(dir, "c0ffee00-0000-4000-8000-000000000001", "subagents")
	writeFile(t, filepath.Join(sub, "agent-d1.jsonl"), jsonl(t,
		cUser("x1", "", "2026-03-01T10:00:06Z", "look", map[string]any{"isSidechain": true, "agentId": "d1"}),
		cAssistant("x2", "x1", "2026-03-01T10:00:07Z", "msg_x", map[string]any{"type": "text", "text": "two callers"}),
	))
	writeFile(t, filepath.Join(sub, "agent-d1.meta.json"), `{"agentType":"Explore","description":"find callers","toolUseId":"toolu_task"}`)
}

func codexWireSession(t *testing.T, env *Env, id string) {
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxContext("2026-03-01T10:00:00Z", "gpt-test"),
		cxMsg("2026-03-01T10:00:01Z", "user", "<environment_context>\n  <cwd>/home/someone/proj</cwd>\n</environment_context>"),
		cxMsg("2026-03-01T10:00:02Z", "user", "rename the topic"),
		cxLine("2026-03-01T10:00:03Z", "response_item", map[string]any{"type": "reasoning", "summary": []any{}, "encrypted_content": "gAAA"}),
		cxCall("2026-03-01T10:00:04Z", "shell", `{"command":["bash","-lc","grep -rn topic ."],"workdir":"/home/someone/proj"}`, "call_1"),
		cxOut("2026-03-01T10:00:05Z", "call_1", `{"output":"a.py:1:topic","metadata":{"exit_code":0,"duration_seconds":0.1}}`),
		cxLine("2026-03-01T10:00:06Z", "response_item", map[string]any{"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_2",
			"input": "*** Begin Patch\n*** Update File: a.py\n@@\n-topic\n+subject\n*** End Patch"}),
		cxLine("2026-03-01T10:00:07Z", "response_item", map[string]any{"type": "custom_tool_call_output", "call_id": "call_2", "output": "Success. Updated a.py"}),
		cxLine("2026-03-01T10:00:08Z", "compacted", map[string]any{"message": "renamed topic to subject",
			"replacement_history": []any{map[string]any{"type": "compaction", "encrypted_content": "gAAA"}}}),
		cxMsg("2026-03-01T10:00:09Z", "user", "thanks"),
		cxMsg("2026-03-01T10:00:10Z", "assistant", "Done."),
	)
}

func TestWireSamples(t *testing.T) {
	env := testEnv(t, nil)
	claudeWireSession(t, env)
	codexID := cxID(42)
	codexWireSession(t, env, codexID)
	got := map[string][]byte{
		"claude.json": sample(t, env, NewService(env, []Parser{claudeParser{}}), "claude", "c0ffee00-0000-4000-8000-000000000001"),
		"codex.json":  sample(t, env, NewService(env, []Parser{codexParser{}}), "codex", codexID),
	}
	for name, b := range got {
		path := filepath.Join("testdata", "wire", name)
		if *updateWire {
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, b, 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("%v (rewrite the samples with -update-wire)", err)
		}
		if !bytes.Equal(want, b) {
			t.Fatalf("%s no longer matches what the parser answers; rewrite it with -update-wire, review the diff, and run the host's tests/unit/test_foreign_wire.py", path)
		}
	}
}
