package sessions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type grokRec2 map[string]any

func grokStore(env *Env) string { return filepath.Join(env.Home, ".grok", "sessions") }

func grokService(env *Env) *Service { return NewService(env, []Parser{grokParser{}}) }

// grokWrite writes one session folder; a nil summary leaves summary.json out.
func grokWrite(t *testing.T, store, folder, id string, summary map[string]any, history ...any) string {
	t.Helper()
	dir := filepath.Join(store, folder, id)
	path := filepath.Join(dir, "chat_history.jsonl")
	writeFile(t, path, jsonl(t, history...))
	if summary != nil {
		b, _ := json.Marshal(summary)
		writeFile(t, filepath.Join(dir, "summary.json"), string(b))
	}
	return path
}

func grokSummaryOf(extra map[string]any) map[string]any {
	s := map[string]any{"agent_name": "main", "created_at": "2026-03-01T10:00:00.5Z", "updated_at": "2026-03-01T10:05:00Z",
		"last_active_at": "2026-03-01T10:06:00.123456789Z", "num_messages": 4, "current_model_id": "grok-test",
		"head_branch": "main", "info": map[string]any{"cwd": "/home/someone/proj", "id": "x"}}
	for k, v := range extra {
		s[k] = v
	}
	return s
}

const grokFolder = "%2Fhome%2Fsomeone%2Fproj"

func gUser(text string) grokRec2 {
	return grokRec2{"type": "user", "content": []any{map[string]any{"type": "text", "text": text}}}
}

func gSynthetic(reason, text string) grokRec2 {
	r := gUser(text)
	r["synthetic_reason"] = reason
	return r
}

func gAssistant(text string, calls ...map[string]any) grokRec2 {
	r := grokRec2{"type": "assistant", "content": text, "model_id": "grok-answer-1"}
	if len(calls) > 0 {
		r["tool_calls"] = calls
	}
	return r
}

func gCall(id, name, args string) map[string]any {
	return map[string]any{"id": id, "name": name, "arguments": args}
}

func gResult(id, text string) grokRec2 {
	return grokRec2{"type": "tool_result", "tool_call_id": id, "content": text}
}

func gReasoning(text string) grokRec2 {
	return grokRec2{"type": "reasoning", "id": "rs", "summary": []any{map[string]any{"type": "summary_text", "text": text}}, "encrypted_content": "zzz"}
}

func TestGrokGroupsAnAnswerAndMapsEachKind(t *testing.T) {
	env := testEnv(t, nil)
	grokWrite(t, grokStore(env), grokFolder, "s1", grokSummaryOf(map[string]any{"generated_title": "Generated name"}),
		grokRec2{"type": "system", "content": strings.Repeat("rules ", 1000)},
		gSynthetic("system_reminder", "<reminder>"),
		gUser("please read it"),
		gReasoning("thinking about it"),
		gAssistant("on it", gCall("call_1", "read_file", `{"path":"a.txt"}`), gCall("call_2", "grep", `{"q":"x"}`)),
		gResult("call_1", "file body"),
		gResult("call_2", "no hits"),
		gReasoning("now the answer"),
		gAssistant("all done"),
	)
	s := grokService(env)
	turns, last := readAll(t, s, "grok", "s1", true, MaxPage)
	var got []string
	for _, tn := range turns {
		got = append(got, tn.Role+":"+kinds(tn))
	}
	want := "system_note:meta:system|system_note:meta:synthetic:system_reminder|user:text|" +
		"assistant:thinking,text,tool_call,tool_call|user:tool_result,tool_result|assistant:thinking,text"
	if strings.Join(got, "|") != want {
		t.Fatalf("turns\n got %s\nwant %s", strings.Join(got, "|"), want)
	}
	note := wire(t, turns[0].Parts[0])["data"].(map[string]any)["text"].(string)
	if len(note) > noteLimit {
		t.Fatalf("system note is %d bytes", len(note))
	}
	call, result := wire(t, turns[3].Parts[2]), wire(t, turns[4].Parts[1])
	args, _ := call["input"].(map[string]any)
	if call["call_id"] != "call_1" || call["name"] != "read_file" || args["path"] != "a.txt" {
		t.Fatalf("call %v", call)
	}
	if result["call_id"] != "call_2" || result["output"] != "no hits" || result["is_error"] != false {
		t.Fatalf("result %v", result)
	}
	h := last.Header
	if h.Title != "Generated name" || h.Messages != 3 || h.Model != "grok-answer-1" || h.Cwd != "/home/someone/proj" ||
		h.Branch != "main" || h.StartedAt.Format("15:04:05") != "10:00:00" || h.UpdatedAt.Format("15:04") != "10:06" {
		t.Fatalf("header %+v", h)
	}
}

func TestGrokTitleAndCwdFallBackWithoutASummary(t *testing.T) {
	env := testEnv(t, nil)
	grokWrite(t, grokStore(env), grokFolder, "bare", nil,
		gSynthetic("project_instructions", "be nice"), gUser("what is in here?"), gAssistant("a folder"))
	s := grokService(env)
	r, err := s.Scan(ScanRequest{Harness: "grok", Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Here) != 1 || r.Here[0].Title != "what is in here?" || r.Here[0].Messages != 2 {
		t.Fatalf("scan %+v", r.Here)
	}
}

func TestGrokCompactionSummaryIsAPartThatCoversTheTurnsBefore(t *testing.T) {
	env := testEnv(t, nil)
	grokWrite(t, grokStore(env), grokFolder, "s1", grokSummaryOf(nil),
		gUser("one"), gAssistant("two"), gUser("three"), gAssistant("four"),
		gSynthetic("compaction_meta", "the story so far"), gUser("five"))
	turns, last := readAll(t, grokService(env), "grok", "s1", true, MaxPage)
	if len(turns) != 6 || last.Header.Flags.Compacted != 1 || last.Header.Messages != 5 {
		t.Fatalf("turns %d header %+v", len(turns), last.Header)
	}
	c := wire(t, turns[4].Parts[0])
	covers, _ := c["covers"].([]any)
	if c["kind"] != "compaction" || c["summary"] != "the story so far" || len(covers) != 2 || covers[0].(float64) != 0 || covers[1].(float64) != 3 {
		t.Fatalf("compaction %v", c)
	}
}

func TestGrokSubagentsAreNestedInTheirParentAndHiddenFromTheListing(t *testing.T) {
	env := testEnv(t, nil)
	store := grokStore(env)
	grokWrite(t, store, grokFolder, "parent", grokSummaryOf(map[string]any{"generated_title": "Parent"}),
		gUser("delegate it"), gAssistant("spawning", gCall("c1", "spawn_subagent", `{}`)), gResult("c1", "started"), gAssistant("collected"))
	grokWrite(t, store, grokFolder, "child", grokSummaryOf(map[string]any{"session_kind": "subagent_fork", "parent_session_id": "parent",
		"agent_name": "explore", "created_at": "2026-03-01T10:00:30Z"}),
		gUser("sub task"), gAssistant("sub answer"))
	// A sub-agent that names no parent has nowhere to nest: it is hidden, not listed as the operator's.
	grokWrite(t, store, grokFolder, "orphan", grokSummaryOf(map[string]any{"session_kind": "subagent"}), gUser("stray"), gAssistant("stray answer"))

	s := grokService(env)
	r, err := s.Scan(ScanRequest{Harness: "grok", Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Here) != 1 || r.Here[0].ID != "parent" || r.Here[0].Flags.Sidechains != 1 {
		t.Fatalf("listing %+v", r.Here)
	}
	if h := s.Harnesses(); len(h) != 1 || h[0].Sessions != 1 {
		t.Fatalf("harnesses %+v", h)
	}

	turns, _ := readAll(t, s, "grok", "parent", true, MaxPage)
	var got []string
	for _, tn := range turns {
		got = append(got, tn.Sidechain+"/"+tn.Role+":"+kinds(tn))
	}
	want := "/user:text|/assistant:text,tool_call|/user:tool_result|/assistant:text|" +
		"child/user:meta:sidechain,text|child/assistant:text"
	if strings.Join(got, "|") != want {
		t.Fatalf("turns\n got %s\nwant %s", strings.Join(got, "|"), want)
	}
	if meta := wire(t, turns[4].Parts[0])["data"].(map[string]any); meta["id"] != "child" || meta["agent_name"] != "explore" {
		t.Fatalf("sidechain meta %v", meta)
	}
	flat, _ := readAll(t, s, "grok", "parent", false, MaxPage)
	if len(flat) != 4 {
		t.Fatalf("without sidechains: %d turns", len(flat))
	}
	// The sub-agent can still be read by its own id.
	own, _ := readAll(t, s, "grok", "child", true, MaxPage)
	if len(own) != 2 {
		t.Fatalf("child read: %d turns", len(own))
	}
}

func TestGrokPlacesASubagentAfterTheLastTurnNotLaterThanItsStart(t *testing.T) {
	at := func(s string) TurnRef { return TurnRef{At: stamp(s)} }
	parent := []TurnRef{at("2026-03-01T10:00:00Z"), at("2026-03-01T10:01:00Z"), at("2026-03-01T10:03:00Z")}
	child := []TurnRef{{Sidechain: "c"}}
	got := grokPlace(parent, child, stamp("2026-03-01T10:02:00Z"))
	if len(got) != 4 || got[2].Sidechain != "c" {
		t.Fatalf("placed %+v", got)
	}
	if got := grokPlace(parent, child, stamp("")); got[3].Sidechain != "c" {
		t.Fatalf("a start never written should go last: %+v", got)
	}
}

func TestGrokSkipsDamagedLinesAndMasksSecrets(t *testing.T) {
	env := testEnv(t, nil)
	token := "ghp_" + strings.Repeat("A1b2C3d4E5", 3) + "A1b2C3"
	path := grokWrite(t, grokStore(env), grokFolder, "s1", grokSummaryOf(nil),
		gUser("push it"), gAssistant("pushing", gCall("c1", "run_terminal_command", `{"command":"git push"}`)))
	body, _ := os.ReadFile(path)
	damaged := string(body) + "garbage that is not json\n" + jsonl(t, gResult("c1", "remote: using "+token))
	writeFile(t, path, damaged)

	cand := Candidate{Harness: "grok", ID: "s1", Root: grokStore(env), Path: path}
	idx, err := grokParser{}.Index(env, &cand, true)
	if err != nil {
		t.Fatal(err)
	}
	if idx.Damaged != 1 || len(idx.Turns) != 3 {
		t.Fatalf("damaged %d turns %d", idx.Damaged, len(idx.Turns))
	}
	turns, last := readAll(t, grokService(env), "grok", "s1", true, MaxPage)
	b, _ := json.Marshal(turns)
	if strings.Contains(string(b), token) || !strings.Contains(string(b), Mask) || last.Masked < 1 {
		t.Fatalf("masked %d: %s", last.Masked, b)
	}
}

func TestGrokPeekReadsTheSummaryAndNotTheHistory(t *testing.T) {
	env := testEnv(t, nil)
	path := grokWrite(t, grokStore(env), grokFolder, "s1", grokSummaryOf(map[string]any{"generated_title": "From the summary"}), gUser("x"))
	// A history that cannot be parsed at all: if Peek touched it, the header would not come out right.
	writeFile(t, path, strings.Repeat("\x00\x01 not records ", 1000))
	c := Candidate{Harness: "grok", ID: "s1", Root: grokStore(env), Path: path}
	h, err := grokParser{}.Peek(env, &c)
	if err != nil {
		t.Fatal(err)
	}
	if h.Title != "From the summary" || h.Cwd != "/home/someone/proj" || h.Messages != 4 || h.Branch != "main" {
		t.Fatalf("peek %+v", h)
	}
}

func TestGrokRootFollowsTheEnvironmentAndLinksAreNotListed(t *testing.T) {
	env := testEnv(t, nil)
	if got := (grokParser{}).Roots(env); len(got) != 1 || got[0] != grokStore(env) {
		t.Fatalf("default roots %v", got)
	}
	env = testEnv(t, map[string]string{"GROK_HOME": "~/grok-alt"})
	store := filepath.Join(env.Home, "grok-alt", "sessions")
	if got := (grokParser{}).Roots(env); got[0] != store {
		t.Fatalf("roots %v", got)
	}
	grokWrite(t, store, grokFolder, "in", grokSummaryOf(nil), gUser("inside"), gAssistant("ok"))
	outside := filepath.Join(env.Home, "outside")
	grokWrite(t, outside, "elsewhere", "out", grokSummaryOf(nil), gUser("outside"), gAssistant("no"))
	// A linked folder of cwds, and a linked session folder inside a real cwd folder.
	if err := os.Symlink(outside, filepath.Join(store, "linked")); err != nil {
		t.Skip("no symbolic links here")
	}
	if err := os.Symlink(filepath.Join(outside, "elsewhere", "out"), filepath.Join(store, grokFolder, "linked-session")); err != nil {
		t.Fatal(err)
	}
	r, err := grokService(env).Scan(ScanRequest{Harness: "grok", Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Here) != 1 || r.Here[0].ID != "in" {
		t.Fatalf("listed %+v", r.Here)
	}
}
