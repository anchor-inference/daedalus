package sessions

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// cxRec is one synthetic Codex record.
type cxRec map[string]any

func cxLine(at, typ string, payload any) cxRec {
	return cxRec{"timestamp": at, "type": typ, "payload": payload}
}

// cxID is a session id in the shape Codex names its rollouts with: 36 characters.
func cxID(n int) string { return fmt.Sprintf("019e0000-0000-7000-8000-%012d", n) }

func cxMeta(id, at, cwd string, extra map[string]any) cxRec {
	p := map[string]any{"id": id, "timestamp": at, "cwd": cwd, "originator": "codex_cli", "cli_version": "0.99.0",
		"source": "cli", "model_provider": "test", "git": map[string]any{"branch": "main", "commit_hash": "abc"},
		"base_instructions": map[string]any{"text": "instructions of the program, long in the real thing"}}
	for k, v := range extra {
		p[k] = v
	}
	return cxLine(at, "session_meta", p)
}

func cxContext(at, model string) cxRec {
	return cxLine(at, "turn_context", map[string]any{"turn_id": "t1", "cwd": "/home/someone/proj", "model": model})
}

func cxMsg(at, role, text string) cxRec {
	kind := "input_text"
	if role == "assistant" {
		kind = "output_text"
	}
	return cxLine(at, "response_item", map[string]any{"type": "message", "role": role,
		"content": []any{map[string]any{"type": kind, "text": text}}})
}

func cxCall(at, name, args, callID string) cxRec {
	return cxLine(at, "response_item", map[string]any{"type": "function_call", "name": name, "arguments": args, "call_id": callID})
}

func cxOut(at, callID string, output any) cxRec {
	return cxLine(at, "response_item", map[string]any{"type": "function_call_output", "call_id": callID, "output": output})
}

func cxItem(at string, item map[string]any) cxRec {
	return cxLine(at, "event_msg", map[string]any{"type": "item_completed", "thread_id": "t", "turn_id": "t1", "item": item})
}

func cxStore(env *Env) string { return filepath.Join(env.Home, ".codex") }

func cxService(env *Env) *Service { return NewService(env, []Parser{codexParser{}}) }

// cxWrite writes a rollout in the dated layout and returns its path.
func cxWrite(t *testing.T, env *Env, id string, records ...any) string {
	t.Helper()
	path := filepath.Join(cxStore(env), "sessions", "2026", "03", "01", "rollout-2026-03-01T10-00-00-"+id+".jsonl")
	writeFile(t, path, jsonl(t, records...))
	return path
}

func cxRead(t *testing.T, s *Service, id string, sidechains bool) []Turn {
	t.Helper()
	turns, _ := readAll(t, s, "codex", id, sidechains, MaxPage)
	return turns
}

func cxScan(t *testing.T, s *Service, path string) ScanReply {
	t.Helper()
	r, err := s.Scan(ScanRequest{Harness: "codex", Path: path})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func wantKinds(t *testing.T, turns []Turn, want ...string) {
	t.Helper()
	var got []string
	for _, turn := range turns {
		got = append(got, turn.Role+":"+kinds(turn))
	}
	if strings.Join(got, " | ") != strings.Join(want, " | ") {
		t.Fatalf("turns\n got  %s\n want %s", strings.Join(got, " | "), strings.Join(want, " | "))
	}
}

func TestCodexOldStyleShellCall(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(1)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxContext("2026-03-01T10:00:00Z", "gpt-test"),
		cxMsg("2026-03-01T10:00:01Z", "user", "<environment_context>\n  <cwd>/home/someone/proj</cwd>\n</environment_context>"),
		cxMsg("2026-03-01T10:00:02Z", "user", "list the files"),
		cxLine("2026-03-01T10:00:03Z", "response_item", map[string]any{"type": "reasoning",
			"summary": []any{map[string]any{"type": "summary_text", "text": "Listing."}}, "encrypted_content": "gAAA"}),
		cxCall("2026-03-01T10:00:04Z", "shell", `{"command":["bash","-lc","ls missing"],"workdir":"/home/someone/proj"}`, "call_1"),
		cxOut("2026-03-01T10:00:05Z", "call_1", `{"output":"ls: cannot access","metadata":{"exit_code":2,"duration_seconds":0.1}}`),
		cxMsg("2026-03-01T10:00:06Z", "assistant", "It does not exist."),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns,
		"system_note:meta:cli", "user:text", "assistant:thinking,tool_call", "user:tool_result", "assistant:text")
	call := wire(t, turns[2].Parts[1])
	if call["name"] != "shell" || call["tool_kind"] != "native" || call["call_id"] != "call_1" {
		t.Fatalf("call: %v", call)
	}
	if cmd := call["input"].(map[string]any)["command"].([]any); cmd[2] != "ls missing" {
		t.Fatalf("the arguments were not carried as JSON: %v", call["input"])
	}
	res := wire(t, turns[3].Parts[0])
	if res["output"] != "ls: cannot access" || res["is_error"] != true {
		t.Fatalf("the exit code did not come out of the output's wrapper: %v", res)
	}
	if turns[2].Model != "gpt-test" {
		t.Fatalf("model: %q", turns[2].Model)
	}
	got := cxScan(t, cxService(env), "/home/someone/proj").Here[0]
	if got.Messages != 2 || got.Title != "list the files" || got.Branch != "main" || got.Model != "gpt-test" || got.Harness != "codex" {
		t.Fatalf("header: %+v", got)
	}
}

func TestCodexToolNamesAndBrokenArguments(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(2)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "go"),
		cxCall("2026-03-01T10:00:02Z", "mcp__docs__search", `{"q":"x"}`, "c1"),
		cxLine("2026-03-01T10:00:02Z", "response_item", map[string]any{"type": "function_call", "name": "send_message",
			"namespace": "collaboration", "arguments": `{"to":"a"}`, "call_id": "c2"}),
		cxCall("2026-03-01T10:00:02Z", "shell", `{"command":`, "c3"),
		cxLine("2026-03-01T10:00:02Z", "response_item", map[string]any{"type": "custom_tool_call", "name": "apply_patch",
			"input": "*** Begin Patch\n*** End Patch", "call_id": "c4"}),
		cxLine("2026-03-01T10:00:02Z", "response_item", map[string]any{"type": "web_search_call", "status": "completed",
			"action": map[string]any{"type": "search", "query": "q"}}),
		cxLine("2026-03-01T10:00:03Z", "event_msg", map[string]any{"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"}),
		cxLine("2026-03-01T10:00:04Z", "something_new", map[string]any{"x": 1}),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:tool_call,tool_call,tool_call,tool_call,tool_call", "user:tool_result",
		"system_note:meta:turn_aborted", "system_note:meta:something_new")
	p := turns[1].Parts
	if m := wire(t, p[0]); m["tool_kind"] != "mcp" || m["server"] != "docs" {
		t.Fatalf("mcp: %v", m)
	}
	if m := wire(t, p[1]); m["tool_kind"] != "custom" || m["server"] != "collaboration" {
		t.Fatalf("namespace: %v", m)
	}
	if m := wire(t, p[2]); m["input"].(map[string]any)["raw"] != `{"command":` {
		t.Fatalf("a broken argument string was lost: %v", m)
	}
	if m := wire(t, p[3]); m["input"].(map[string]any)["input"] != "*** Begin Patch\n*** End Patch" {
		t.Fatalf("patch input: %v", m)
	}
	if m := wire(t, p[4]); m["name"] != "web_search" {
		t.Fatalf("web search: %v", m)
	}
	if wire(t, turns[3].Parts[0])["data"].(map[string]any)["reason"] != "interrupted" {
		t.Fatal("the reason of an abort was lost")
	}
}

func TestCodexCodeModeTakesTheMeaningFromItems(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(3)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "run the tests"),
		cxLine("2026-03-01T10:00:02Z", "response_item", map[string]any{"type": "custom_tool_call", "status": "completed",
			"name": "exec", "call_id": "x1", "input": "const r = await tools.exec_command({cmd: 'pytest -q'}); text(r)"}),
		cxItem("2026-03-01T10:00:03Z", map[string]any{"type": "CommandExecution", "id": "i1", "command": []string{"bash", "-lc", "pytest -q"},
			"cwd": "/home/someone/proj", "status": "failed", "aggregated_output": "1 failed", "exit_code": 1}),
		cxItem("2026-03-01T10:00:04Z", map[string]any{"type": "FileChange", "id": "i2", "status": "completed", "stdout": "Success",
			"changes": map[string]any{"/home/someone/proj/a.py": map[string]any{"type": "update", "unified_diff": "@@ -1 +1 @@", "move_path": nil}}}),
		cxItem("2026-03-01T10:00:05Z", map[string]any{"type": "McpToolCall", "id": "i3", "server": "docs", "tool": "search",
			"arguments": map[string]any{"q": "x"}, "status": "completed",
			"result": map[string]any{"content": []any{map[string]any{"type": "text", "text": "a hit"}}, "isError": false}}),
		cxItem("2026-03-01T10:00:05Z", map[string]any{"type": "Reasoning", "id": "r1", "summary_text": []string{"x"}}),
		cxItem("2026-03-01T10:00:05Z", map[string]any{"type": "AgentMessage", "id": "m1", "content": []any{}}),
		cxLine("2026-03-01T10:00:06Z", "response_item", map[string]any{"type": "custom_tool_call_output", "call_id": "x1",
			"output": []any{map[string]any{"type": "input_text", "text": "Script completed"}}}),
		cxMsg("2026-03-01T10:00:07Z", "assistant", "One test fails."),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:meta:codex_exec,tool_call", "user:tool_result", "assistant:tool_call", "user:tool_result",
		"assistant:tool_call", "user:tool_result,meta:codex_exec_output", "assistant:text")
	if m := wire(t, turns[1].Parts[0]); !strings.Contains(m["data"].(map[string]any)["code"].(string), "tools.exec_command") || m["data"].(map[string]any)["call_id"] != "x1" {
		t.Fatalf("exec meta: %v", m)
	}
	call := wire(t, turns[1].Parts[1])
	in := call["input"].(map[string]any)
	if call["name"] != "exec_command" || call["call_id"] != "i1" || in["command"] != "pytest -q" || in["cwd"] != "/home/someone/proj" {
		t.Fatalf("command call: %v", call)
	}
	if r := wire(t, turns[2].Parts[0]); r["call_id"] != "i1" || r["output"] != "1 failed" || r["is_error"] != true {
		t.Fatalf("command result: %v", r)
	}
	if c := wire(t, turns[3].Parts[0]); c["name"] != "apply_patch" || c["input"].(map[string]any)["changes"] == nil {
		t.Fatalf("patch call: %v", c)
	}
	if r := wire(t, turns[4].Parts[0]); r["output"] != "Success" || r["is_error"] != false {
		t.Fatalf("patch result: %v", r)
	}
	if c := wire(t, turns[5].Parts[0]); c["name"] != "search" || c["tool_kind"] != "mcp" || c["server"] != "docs" {
		t.Fatalf("mcp call: %v", c)
	}
	if r := wire(t, turns[6].Parts[0]); r["output"] != "a hit" {
		t.Fatalf("mcp result: %v", r)
	}
	if m := wire(t, turns[6].Parts[1]); m["data"].(map[string]any)["text"] != "Script completed" {
		t.Fatalf("exec output: %v", m)
	}
	// The exec output is a note, not a result: no tool_result names the exec call.
	for _, turn := range turns {
		for _, p := range turn.Parts {
			if p.Kind == KindToolResult && p.CallID == "x1" {
				t.Fatal("the output of the JavaScript became a tool result")
			}
		}
	}
	if cxScan(t, cxService(env), "/home/someone/proj").Here[0].Messages != 2 {
		t.Fatal("messages should be the request and the answer")
	}
}

func TestCodexOtherItemKinds(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(4)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "go"),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "WebSearch", "id": "w1", "query": "go", "action": map[string]any{"type": "search"}}),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "ImageView", "id": "v1", "path": "/home/someone/proj/a.png"}),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "Plan", "id": "p1", "text": "1. do"}),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "Extension", "id": "e1", "kind": "web", "query": "q",
			"results": []any{map[string]any{"title": "T", "url": "http://127.0.0.1/x", "snippet": "long"}}}),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "EnteredReviewMode", "id": "m1"}),
		cxItem("2026-03-01T10:00:02Z", map[string]any{"type": "Brand", "id": "m2"}),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:tool_call", "user:tool_result", "assistant:tool_call", "user:tool_result", "assistant:tool_call", "user:tool_result",
		"assistant:tool_call", "user:tool_result,meta:review_entered,meta:item:Brand")
	names := []string{}
	for _, i := range []int{1, 3, 5, 7} {
		names = append(names, turns[i].Parts[0].Name)
	}
	if strings.Join(names, ",") != "web_search,view_image,update_plan,extension:web" {
		t.Fatalf("names: %v", names)
	}
	if r := wire(t, turns[8].Parts[0]); !strings.Contains(r["output"].(string), "http://127.0.0.1/x") || strings.Contains(r["output"].(string), "long") {
		t.Fatalf("extension result should be title and url only: %v", r)
	}
}

func TestCodexReasoningIsOnlyKeptWhenItSaysSomething(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(5)
	reasoning := func(at string, summary []any, enc string) cxRec {
		p := map[string]any{"type": "reasoning", "summary": summary, "content": nil}
		if enc != "" {
			p["encrypted_content"] = enc
		}
		return cxLine(at, "response_item", p)
	}
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "think"),
		reasoning("2026-03-01T10:00:02Z", []any{map[string]any{"type": "summary_text", "text": "First."}, map[string]any{"type": "summary_text", "text": "Second."}}, "gAAA"),
		reasoning("2026-03-01T10:00:03Z", []any{}, "gBBB"),
		reasoning("2026-03-01T10:00:04Z", []any{}, ""),
		cxMsg("2026-03-01T10:00:05Z", "assistant", "ok"),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:thinking,thinking,text")
	a, b := wire(t, turns[1].Parts[0]), wire(t, turns[1].Parts[1])
	if a["text"] != "First.\n\nSecond." || a["encrypted"] != false {
		t.Fatalf("summary: %v", a)
	}
	if b["text"] != "" || b["encrypted"] != true {
		t.Fatalf("encrypted only: %v", b)
	}
}

func TestCodexNoiseBecomesNotes(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(6)
	image := cxLine("2026-03-01T10:00:06Z", "response_item", map[string]any{"type": "message", "role": "user", "content": []any{
		map[string]any{"type": "input_text", "text": "<image name=[Image #1]>"},
		map[string]any{"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
		map[string]any{"type": "input_text", "text": "</image>"},
		map[string]any{"type": "input_text", "text": "what is this"}}})
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "developer", "<permissions instructions>sandbox</permissions instructions>"),
		cxMsg("2026-03-01T10:00:02Z", "user", "# AGENTS.md instructions for /home/someone/proj\n\n<INSTRUCTIONS>be brief</INSTRUCTIONS>"),
		cxMsg("2026-03-01T10:00:03Z", "user", "<environment_context>\n</environment_context>"),
		cxMsg("2026-03-01T10:00:04Z", "user", "<subagent_notification>done</subagent_notification>"),
		cxMsg("2026-03-01T10:00:05Z", "user", "the real request"),
		image,
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "system_note:meta:developer", "system_note:meta:cli", "system_note:meta:cli", "system_note:meta:cli", "user:text", "user:text,image")
	if m := wire(t, turns[1].Parts[0]); !strings.Contains(m["data"].(map[string]any)["text"].(string), "AGENTS.md") {
		t.Fatalf("note: %v", m)
	}
	pic := wire(t, turns[5].Parts[1])
	if pic["mime"] != "image/png" || pic["data_b64"] != "iVBORw0KGgo=" || turns[5].Parts[0].Text != "what is this" {
		t.Fatalf("image: %v, text %q", pic, turns[5].Parts[0].Text)
	}
	h := cxScan(t, cxService(env), "/home/someone/proj").Here[0]
	if h.Messages != 2 || h.Title != "the real request" {
		t.Fatalf("the notes must not count or title the session: %+v", h)
	}
}

func TestCodexCompactionCoversWhatItReplaced(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(7)
	compacted := func(at, message string) cxRec {
		return cxLine(at, "compacted", map[string]any{"message": message, "replacement_history": []any{map[string]any{"type": "compaction", "encrypted_content": "gAAA"}}})
	}
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "one"),
		cxMsg("2026-03-01T10:00:02Z", "assistant", "two"),
		cxMsg("2026-03-01T10:00:03Z", "user", "three"),
		compacted("2026-03-01T10:00:04Z", ""),
		cxMsg("2026-03-01T10:00:05Z", "user", "four"),
		cxMsg("2026-03-01T10:00:06Z", "assistant", "five"),
		compacted("2026-03-01T10:00:07Z", "the gist"),
		cxMsg("2026-03-01T10:00:08Z", "user", "six"),
	)
	s := cxService(env)
	turns := cxRead(t, s, id, true)
	wantKinds(t, turns, "user:text", "assistant:text", "user:text", "system_note:compaction", "user:text", "assistant:text", "system_note:compaction", "user:text")
	first, second := wire(t, turns[3].Parts[0]), wire(t, turns[6].Parts[0])
	if c := first["covers"].([]any); c[0] != 0.0 || c[1] != 2.0 || first["auto"] != true {
		t.Fatalf("first: %v", first)
	}
	if c := second["covers"].([]any); c[0] != 4.0 || c[1] != 5.0 || second["summary"] != "the gist" {
		t.Fatalf("second: %v", second)
	}
	if got := cxScan(t, s, "/home/someone/proj").Here[0].Flags.Compacted; got != 2 {
		t.Fatalf("compacted: %d", got)
	}
}

func TestCodexTitleComesFromTheIndexThenTheFirstPrompt(t *testing.T) {
	env := testEnv(t, nil)
	named, plain := cxID(8), cxID(9)
	cxWrite(t, env, named, cxMeta(named, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "first words"))
	cxWrite(t, env, plain, cxMeta(plain, "2026-03-01T11:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T11:00:01Z", "user", "other words"))
	writeFile(t, filepath.Join(cxStore(env), "session_index.jsonl"), jsonl(t,
		cxRec{"id": named, "thread_name": "Old name", "updated_at": "2026-03-01T10:05:00Z"},
		cxRec{"id": named, "thread_name": "Renamed thread", "updated_at": "2026-03-01T10:06:00Z"},
		cxRec{"id": "someone-else", "thread_name": "Not ours"}))
	s := cxService(env)
	titles := map[string]string{}
	for _, h := range cxScan(t, s, "/home/someone/proj").Here {
		titles[h.ID] = h.Title
	}
	if titles[named] != "Renamed thread" || titles[plain] != "other words" {
		t.Fatalf("titles: %v", titles)
	}
	// Renaming appends; the cache must notice the file grew.
	f, err := os.OpenFile(filepath.Join(cxStore(env), "session_index.jsonl"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	fmt.Fprintf(f, `{"id":%q,"thread_name":"Named later"}`+"\n", plain)
	f.Close()
	h, err := codexParser{}.Peek(env, &Candidate{Root: filepath.Join(cxStore(env), "sessions"), ID: plain,
		Path: filepath.Join(cxStore(env), "sessions", "2026", "03", "01", "rollout-2026-03-01T10-00-00-"+plain+".jsonl")})
	if err != nil || h.Title != "Named later" {
		t.Fatalf("title %q err %v", h.Title, err)
	}
}

func TestCodexSubAgentsAreInlinedNotListed(t *testing.T) {
	env := testEnv(t, nil)
	parent, child := cxID(10), cxID(11)
	cxWrite(t, env, parent,
		cxMeta(parent, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "split the work"),
		cxCall("2026-03-01T10:00:02Z", "spawn_agent", `{"message":"do it"}`, "spawn_1"),
		cxOut("2026-03-01T10:00:03Z", "spawn_1", `{"agent_id":"`+child+`","nickname":"Ada"}`),
		cxMsg("2026-03-01T10:00:10Z", "assistant", "They are working."),
	)
	cxWrite(t, env, child,
		cxMeta(child, "2026-03-01T10:00:04Z", "/home/someone/proj", map[string]any{"source": map[string]any{"subagent": map[string]any{
			"thread_spawn": map[string]any{"parent_thread_id": parent, "depth": 1, "agent_path": "/root/ada", "agent_nickname": "Ada", "agent_role": "worker"}}}}),
		cxMsg("2026-03-01T10:00:05Z", "user", "do it"),
		cxMsg("2026-03-01T10:00:06Z", "assistant", "done it"),
	)
	s := cxService(env)
	scan := cxScan(t, s, "/home/someone/proj")
	if len(scan.Here) != 1 || scan.Here[0].ID != parent || scan.Here[0].Flags.Sidechains != 1 {
		t.Fatalf("the sub-agent must be listed inside its parent only: %+v", scan.Here)
	}
	if all := cxScan(t, s, ""); len(all.Folders) != 1 || all.Folders[0].Sessions != 1 {
		t.Fatalf("folders: %+v", all.Folders)
	}
	turns := cxRead(t, s, parent, true)
	wantKinds(t, turns, "user:text", "assistant:tool_call", "user:tool_result", "user:meta:sidechain,text", "assistant:text", "assistant:text")
	for i, want := range []string{"", "", "", child, child, ""} {
		if turns[i].Sidechain != want {
			t.Fatalf("turn %d sidechain %q, want %q", i, turns[i].Sidechain, want)
		}
	}
	data := wire(t, turns[3].Parts[0])["data"].(map[string]any)
	if data["id"] != child || data["agent_nickname"] != "Ada" || data["agent_role"] != "worker" || data["agent_path"] != "/root/ada" || data["parent_call"] != "spawn_1" {
		t.Fatalf("sidechain meta: %v", data)
	}
	if got := cxRead(t, s, parent, false); len(got) != 4 {
		t.Fatalf("without sidechains the parent has 4 turns, got %d", len(got))
	}
	h, err := codexParser{}.Peek(env, &Candidate{Root: filepath.Join(cxStore(env), "sessions"), ID: child,
		Path: filepath.Join(cxStore(env), "sessions", "2026", "03", "01", "rollout-2026-03-01T10-00-00-"+child+".jsonl")})
	if err != nil || h.parent != parent {
		t.Fatalf("parent %q err %v", h.parent, err)
	}
}

func TestCodexAForkIsNotASubAgent(t *testing.T) {
	env := testEnv(t, nil)
	orig, fork := cxID(12), cxID(13)
	cxWrite(t, env, orig, cxMeta(orig, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "a"))
	cxWrite(t, env, fork, cxMeta(fork, "2026-03-01T10:00:00Z", "/home/someone/proj", map[string]any{"forked_from_id": orig}), cxMsg("2026-03-01T10:00:01Z", "user", "b"))
	if got := cxScan(t, cxService(env), "/home/someone/proj").Here; len(got) != 2 {
		t.Fatalf("a fork is its own session: %d listed", len(got))
	}
}

func TestCodexCollabItemsRepeatingACallAreDropped(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(14)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "wait"),
		cxCall("2026-03-01T10:00:02Z", "wait_agent", `{"targets":["a"]}`, "w1"),
		cxItem("2026-03-01T10:00:03Z", map[string]any{"type": "CollabAgentToolCall", "id": "w1", "tool": "wait_agent", "status": "completed"}),
		cxOut("2026-03-01T10:00:04Z", "w1", "agent finished"),
		cxItem("2026-03-01T10:00:05Z", map[string]any{"type": "CollabAgentToolCall", "id": "z9", "tool": "close_agent", "status": "completed"}),
		cxItem("2026-03-01T10:00:06Z", map[string]any{"type": "CommandExecution", "id": "w1", "command": []string{"ls"}, "exit_code": 0}),
	)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:tool_call", "user:tool_result", "assistant:tool_call", "user:tool_result")
	if c := wire(t, turns[1].Parts[0]); c["call_id"] != "w1" {
		t.Fatalf("%v", c)
	}
	if c := wire(t, turns[3].Parts[0]); c["call_id"] != "z9" || c["name"] != "close_agent" || c["tool_kind"] != "custom" {
		t.Fatalf("%v", c)
	}
}

func TestCodexDamagedAndUnfinishedLinesAreSkipped(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(15)
	body := jsonl(t,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "before")) +
		"{this is not json\n" +
		jsonl(t, cxMsg("2026-03-01T10:00:03Z", "assistant", "after")) +
		`{"timestamp":"2026-03-01T10:00:04Z","type":"response_item","payload":{"type":"mess`
	path := filepath.Join(cxStore(env), "sessions", "2026", "03", "01", "rollout-2026-03-01T10-00-00-"+id+".jsonl")
	writeFile(t, path, body)
	turns := cxRead(t, cxService(env), id, true)
	wantKinds(t, turns, "user:text", "assistant:text")
}

func TestCodexSecretsInOutputAreMasked(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(16)
	token := "ghp_" + strings.Repeat("aB3dE5", 6)
	cxWrite(t, env, id,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil),
		cxMsg("2026-03-01T10:00:01Z", "user", "show the env"),
		cxCall("2026-03-01T10:00:02Z", "shell", `{"command":["env"]}`, "c1"),
		cxOut("2026-03-01T10:00:03Z", "c1", "GITHUB_TOKEN="+token+"\nHOME=/home/someone"),
	)
	turns, last := readAll(t, cxService(env), "codex", id, true, MaxPage)
	out := turns[2].Parts[0].Output
	if strings.Contains(out, token) || !strings.Contains(out, Mask) || last.Masked == 0 {
		t.Fatalf("not masked (masked=%d): %q", last.Masked, out)
	}
}

func TestCodexScanGroupsByFolder(t *testing.T) {
	env := testEnv(t, nil)
	for i, cwd := range []string{"/home/someone/a", "/home/someone/a/sub", "/home/someone/b", "/home/someone/a"} {
		id := cxID(20 + i)
		cxWrite(t, env, id, cxMeta(id, fmt.Sprintf("2026-03-01T1%d:00:00Z", i), cwd, nil), cxMsg("2026-03-01T12:00:01Z", "user", "hi"))
	}
	s := cxService(env)
	all := cxScan(t, s, "")
	if len(all.Folders) != 3 || len(all.Here) != 0 {
		t.Fatalf("folders %+v here %d", all.Folders, len(all.Here))
	}
	top := cxScan(t, s, "/home/someone")
	if len(top.Children) != 2 || top.Children[0].Name != "a" || top.Children[0].Sessions != 3 || top.Children[1].Name != "b" {
		t.Fatalf("children: %+v", top.Children)
	}
	a := cxScan(t, s, "/home/someone/a")
	if len(a.Here) != 2 || len(a.Children) != 1 || a.Children[0].Name != "sub" {
		t.Fatalf("a: here %d children %+v", len(a.Here), a.Children)
	}
}

func TestCodexPagingCutsABigOutput(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(30)
	records := []any{cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "dump it")}
	for i := 0; i < 6; i++ {
		call := fmt.Sprintf("c%d", i)
		records = append(records, cxCall("2026-03-01T10:00:02Z", "shell", `{"command":["cat","big"]}`, call),
			cxOut("2026-03-01T10:00:03Z", call, strings.Repeat("line of output\n", 20000)))
	}
	records = append(records, cxMsg("2026-03-01T10:00:04Z", "assistant", "that was long"))
	cxWrite(t, env, id, records...)
	s := cxService(env)
	pages := 0
	var all []Turn
	for from := int64(0); ; {
		r, err := s.Read(ReadRequest{Harness: "codex", ID: id, From: from, MaxBytes: 16384})
		if err != nil {
			t.Fatal(err)
		}
		reply := r.(ReadReply)
		pages++
		all = append(all, reply.Turns...)
		if reply.Done {
			break
		}
		from = reply.Next
	}
	if len(all) != 14 || pages < 2 {
		t.Fatalf("turns %d pages %d", len(all), pages)
	}
	for i := 2; i <= 12; i += 2 {
		res := all[i].Parts[0]
		if !res.Truncated || len(res.Output) > 16384 || !strings.HasSuffix(res.Output, cutMark) {
			t.Fatalf("turn %d truncated=%v len=%d", i, res.Truncated, len(res.Output))
		}
	}
}

func TestCodexHomeFollowsTheEnvironment(t *testing.T) {
	env := testEnv(t, map[string]string{"CODEX_HOME": "~/elsewhere"})
	here, there := cxID(40), cxID(41)
	cxWrite(t, env, here, cxMeta(here, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "default home"))
	path := filepath.Join(env.Home, "elsewhere", "sessions", "2026", "03", "01", "rollout-2026-03-01T10-00-00-"+there+".jsonl")
	writeFile(t, path, jsonl(t, cxMeta(there, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "chosen home")))
	writeFile(t, filepath.Join(env.Home, "elsewhere", "session_index.jsonl"), jsonl(t, cxRec{"id": there, "thread_name": "From the chosen home"}))
	got := cxScan(t, cxService(env), "/home/someone/proj").Here
	if len(got) != 1 || got[0].ID != there || got[0].Title != "From the chosen home" {
		t.Fatalf("%+v", got)
	}
}

func TestCodexLinksOutOfTheStoreAreNotListedOrRead(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(50)
	outside := filepath.Join(filepath.Dir(env.Home), "outside")
	writeFile(t, filepath.Join(outside, "rollout-2026-03-01T10-00-00-"+id+".jsonl"), jsonl(t,
		cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "secret words")))
	day := filepath.Join(cxStore(env), "sessions", "2026", "03", "01")
	if err := os.MkdirAll(day, 0o700); err != nil {
		t.Fatal(err)
	}
	// A link to a file, and a link to a folder of rollouts.
	if err := os.Symlink(filepath.Join(outside, "rollout-2026-03-01T10-00-00-"+id+".jsonl"), filepath.Join(day, "rollout-2026-03-01T10-00-00-"+id+".jsonl")); err != nil {
		t.Skip("no symbolic links here")
	}
	if err := os.Symlink(outside, filepath.Join(cxStore(env), "sessions", "linked")); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(cxStore(env), "auth.json"), `{"token":"x"}`)
	if err := os.Symlink(filepath.Join(cxStore(env), "auth.json"), filepath.Join(day, "rollout-2026-03-01T10-00-00-"+cxID(51)+".jsonl")); err != nil {
		t.Fatal(err)
	}
	s := cxService(env)
	if all := cxScan(t, s, ""); len(all.Folders) != 0 {
		t.Fatalf("a link was listed: %+v", all.Folders)
	}
	if _, err := s.Read(ReadRequest{Harness: "codex", ID: id}); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a link out of the store was read: %v", err)
	}
	// Even named directly, the open refuses it: the resolved path leaves the root.
	if _, err := (codexParser{}).Peek(env, &Candidate{Root: filepath.Join(cxStore(env), "sessions"), ID: id,
		Path: filepath.Join(day, "rollout-2026-03-01T10-00-00-"+id+".jsonl")}); !errors.Is(err, ErrForbidden) {
		t.Fatalf("peek followed the link: %v", err)
	}
}

func TestCodexDiscoversTheArchiveAndSkipsCompressedRollouts(t *testing.T) {
	env := testEnv(t, nil)
	live, archived := cxID(60), cxID(61)
	cxWrite(t, env, live, cxMeta(live, "2026-03-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-03-01T10:00:01Z", "user", "live"))
	writeFile(t, filepath.Join(cxStore(env), "archived_sessions", "rollout-2026-02-01T10-00-00-"+archived+".jsonl"),
		jsonl(t, cxMeta(archived, "2026-02-01T10:00:00Z", "/home/someone/proj", nil), cxMsg("2026-02-01T10:00:01Z", "user", "archived")))
	writeFile(t, filepath.Join(cxStore(env), "archived_sessions", "rollout-2025-01-01T10-00-00-"+cxID(62)+".jsonl.zst"), "not read")
	got := cxScan(t, cxService(env), "/home/someone/proj").Here
	if len(got) != 2 {
		t.Fatalf("%+v", got)
	}
	turns := cxRead(t, cxService(env), archived, true)
	wantKinds(t, turns, "user:text")
}

func TestCodexPeekReadsTheHeadAndTheTailOnly(t *testing.T) {
	env := testEnv(t, nil)
	id := cxID(70)
	// A session_meta far longer than the first window, then a body larger than the tail window.
	meta := cxMeta(id, "2026-03-01T10:00:00Z", "/home/someone/proj", map[string]any{
		"base_instructions": map[string]any{"text": strings.Repeat("rules ", 50000)}})
	records := []any{meta, cxContext("2026-03-01T10:00:00Z", "model-early"), cxMsg("2026-03-01T10:00:01Z", "user", "the first request")}
	for i := 0; i < 400; i++ {
		records = append(records, cxLine("2026-03-01T10:30:00Z", "event_msg", map[string]any{"type": "token_count", "pad": strings.Repeat("x", 400)}))
	}
	records = append(records, cxContext("2026-03-01T11:00:00Z", "model-late"), cxLine("2026-03-01T11:00:01Z", "event_msg", map[string]any{"type": "task_complete"}))
	path := cxWrite(t, env, id, records...)
	st, _ := os.Stat(path)
	h, err := codexParser{}.Peek(env, &Candidate{Root: filepath.Join(cxStore(env), "sessions"), ID: id, Path: path, Size: st.Size()})
	if err != nil {
		t.Fatal(err)
	}
	if h.Cwd != "/home/someone/proj" || h.Branch != "main" || h.version != "0.99.0" || h.Model != "model-late" || h.firstPrompt != "the first request" {
		t.Fatalf("%+v", h)
	}
	if got := h.UpdatedAt.UTC().Format("15:04:05"); got != "11:00:01" {
		t.Fatalf("updated %s", got)
	}
	if got := h.StartedAt.UTC().Format("15:04:05"); got != "10:00:00" {
		t.Fatalf("started %s", got)
	}
}

func TestCodexAPageOfAPaginatedThreadIsReadUnderItsOwnName(t *testing.T) {
	env := testEnv(t, nil)
	thread, page := cxID(70), cxID(71)
	cxWrite(t, env, thread,
		cxMeta(thread, "2026-03-01T10:00:00Z", "/home/someone/proj", map[string]any{"history_mode": "paginated"}),
		cxMsg("2026-03-01T10:00:01Z", "user", "first page"),
		cxMsg("2026-03-01T10:00:02Z", "assistant", "one"),
	)
	cxWrite(t, env, thread+"_"+page,
		cxMeta(thread, "2026-03-01T11:00:00Z", "/home/someone/proj", map[string]any{"history_mode": "paginated",
			"history_base": map[string]any{"thread_id": thread, "end_byte_offset": 100, "end_ordinal_exclusive": 2}}),
		cxMsg("2026-03-01T11:00:01Z", "user", "second page"),
		cxMsg("2026-03-01T11:00:02Z", "assistant", "two"),
		cxMsg("2026-03-01T11:00:03Z", "user", "more"),
		cxMsg("2026-03-01T11:00:04Z", "assistant", "three"),
	)
	s := cxService(env)
	ids := map[string]int{}
	for _, h := range cxScan(t, s, "/home/someone/proj").Here {
		ids[h.ID] = h.Messages
	}
	if len(ids) != 2 || ids[thread] != 2 || ids[page] != 4 {
		t.Fatalf("listed %v", ids)
	}
	if got := cxRead(t, s, page, true); len(got) != 4 {
		t.Fatalf("the page read %d turns", len(got))
	}
	if got := cxRead(t, s, thread, true); len(got) != 2 {
		t.Fatalf("the first file read %d turns", len(got))
	}
}
