package sessions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

type piRec map[string]any

func piStore(env *Env) string { return filepath.Join(env.Home, ".pi", "agent", "sessions") }

func piService(env *Env) *Service { return NewService(env, []Parser{piParser{}}) }

func piHeader(id string) piRec {
	return piRec{"type": "session", "version": 3, "id": id, "timestamp": "2026-03-01T10:00:00.000Z", "cwd": "/home/someone/proj"}
}

func piEntry(typ, id, parent, at string, extra piRec) piRec {
	r := piRec{"type": typ, "id": id, "parentId": nilIfEmpty(parent), "timestamp": at}
	for k, v := range extra {
		r[k] = v
	}
	return r
}

func piMsg(id, parent, at, role string, content any, extra piRec) piRec {
	m := map[string]any{"role": role, "content": content}
	for k, v := range extra {
		m[k] = v
	}
	return piEntry("message", id, parent, at, piRec{"message": m})
}

func piText(s string) []any { return []any{map[string]any{"type": "text", "text": s}} }

func piAssistant(id, parent, at string, blocks ...any) piRec {
	return piMsg(id, parent, at, "assistant", blocks, piRec{"model": "pi-model-1", "provider": "test",
		"usage": map[string]any{"input": 10, "output": 5, "cacheRead": 2}})
}

func piWrite(t *testing.T, env *Env, name string, records ...any) string {
	t.Helper()
	path := filepath.Join(piStore(env), "--home-someone-proj--", "2026-03-01T10-00-00-000Z_"+name+".jsonl")
	writeFile(t, path, jsonl(t, records...))
	return path
}

func TestPiFollowsTheActiveBranchAndMapsEachKind(t *testing.T) {
	env := testEnv(t, nil)
	piWrite(t, env, "s1",
		piHeader("s1"),
		piEntry("thinking_level_change", "a", "", "2026-03-01T10:00:00Z", piRec{"thinkingLevel": "high"}),
		piEntry("model_change", "b", "a", "2026-03-01T10:00:00Z", piRec{"provider": "test", "modelId": "pi-model-1"}),
		piMsg("c", "b", "2026-03-01T10:00:01Z", "user", piText("first ask"), nil),
		piAssistant("d", "c", "2026-03-01T10:00:02Z",
			map[string]any{"type": "thinking", "thinking": "hmm", "thinkingSignature": "sig"},
			map[string]any{"type": "text", "text": "looking"},
			map[string]any{"type": "toolCall", "id": "call_1", "name": "read", "arguments": map[string]any{"path": "a.txt"}}),
		// The abandoned branch: written before the live one, but not on the path from the last entry.
		piMsg("dead", "c", "2026-03-01T10:00:03Z", "user", piText("abandoned prompt"), nil),
		piMsg("e", "d", "2026-03-01T10:00:04Z", "toolResult", piText("file body"), piRec{"toolCallId": "call_1", "toolName": "read", "isError": true}),
		piAssistant("f", "e", "2026-03-01T10:00:05Z", map[string]any{"type": "text", "text": "done"}),
		piEntry("branch_summary", "g", "f", "2026-03-01T10:00:06Z", piRec{"fromId": "dead", "summary": "tried something else"}),
		piEntry("context_edit", "h", "g", "2026-03-01T10:00:07Z", piRec{"edit": "trim"}),
	)
	s := piService(env)
	turns, last := readAll(t, s, "pi", "s1", true, MaxPage)
	var got []string
	for _, tn := range turns {
		got = append(got, tn.Role+":"+kinds(tn))
	}
	want := "system_note:meta:thinking_level_change|system_note:meta:model_change|user:text|assistant:thinking,text,tool_call|" +
		"user:tool_result|assistant:text|system_note:meta:branch_summary|system_note:meta:context_edit"
	if strings.Join(got, "|") != want {
		t.Fatalf("turns\n got %s\nwant %s", strings.Join(got, "|"), want)
	}
	if turns[3].Model != "pi-model-1" || turns[3].Usage == nil || turns[3].Usage.Input != 10 || turns[3].Usage.CacheRead != 2 {
		t.Fatalf("assistant turn %+v", turns[3])
	}
	call, result := wire(t, turns[3].Parts[2]), wire(t, turns[4].Parts[0])
	args, _ := call["input"].(map[string]any)
	if call["call_id"] != "call_1" || call["name"] != "read" || args["path"] != "a.txt" || call["tool_kind"] != "native" {
		t.Fatalf("call %v", call)
	}
	if result["call_id"] != "call_1" || result["output"] != "file body" || result["is_error"] != true {
		t.Fatalf("result %v", result)
	}
	if last.Header.Messages != 3 || last.Header.Model != "pi-model-1" || last.Header.Cwd != "/home/someone/proj" {
		t.Fatalf("header %+v", last.Header)
	}
	b, _ := json.Marshal(turns)
	if strings.Contains(string(b), "abandoned prompt") || strings.Contains(string(b), "sig") {
		t.Fatalf("a dead branch or a signature crossed: %s", b)
	}
}

func TestPiTitlePrecedence(t *testing.T) {
	env := testEnv(t, nil)
	piWrite(t, env, "plain", piHeader("plain"),
		piMsg("a", "", "2026-03-01T10:00:01Z", "user", piText("fix the build"), nil),
		piAssistant("b", "a", "2026-03-01T10:00:02Z", map[string]any{"type": "text", "text": "ok"}))
	// A rename is on the path: the turns after it must still be reached, and it names the session
	// by its last name, not its first.
	piWrite(t, env, "named", piHeader("named"),
		piMsg("a", "", "2026-03-01T10:00:01Z", "user", piText("fix the build"), nil),
		piEntry("session_info", "n1", "a", "2026-03-01T10:00:02Z", piRec{"name": "First name"}),
		piMsg("b", "n1", "2026-03-01T10:00:03Z", "user", "a plain string prompt", nil),
		piEntry("session_info", "n2", "b", "2026-03-01T10:00:04Z", piRec{"name": "Second name"}))
	s := piService(env)
	r, err := s.Scan(ScanRequest{Harness: "pi", Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	titles := map[string]string{}
	for _, h := range r.Here {
		titles[h.ID] = h.Title
	}
	if titles["plain"] != "fix the build" || titles["named"] != "Second name" {
		t.Fatalf("titles %v", titles)
	}
	turns, _ := readAll(t, s, "pi", "named", true, MaxPage)
	if len(turns) != 2 || kinds(turns[1]) != "text" {
		t.Fatalf("a rename cut or joined the history: %d turns", len(turns))
	}
}

func TestPiCompactionCoversWhatItReplacedAndNotWhatItKept(t *testing.T) {
	env := testEnv(t, nil)
	piWrite(t, env, "s1", piHeader("s1"),
		piMsg("u0", "", "2026-03-01T10:00:01Z", "user", piText("one"), nil),
		piAssistant("a1", "u0", "2026-03-01T10:00:02Z", map[string]any{"type": "text", "text": "two"}),
		piMsg("u2", "a1", "2026-03-01T10:00:03Z", "user", piText("three"), nil),
		piAssistant("a3", "u2", "2026-03-01T10:00:04Z", map[string]any{"type": "text", "text": "four"}),
		piEntry("compaction", "c4", "a3", "2026-03-01T10:00:05Z", piRec{"summary": "the story so far", "firstKeptEntryId": "u2", "tokensBefore": 900}),
		piMsg("u5", "c4", "2026-03-01T10:00:06Z", "user", piText("five"), nil))
	turns, last := readAll(t, piService(env), "pi", "s1", true, MaxPage)
	if len(turns) != 6 || last.Header.Flags.Compacted != 1 {
		t.Fatalf("turns %d header %+v", len(turns), last.Header)
	}
	c := wire(t, turns[4].Parts[0])
	covers, _ := c["covers"].([]any)
	if c["kind"] != "compaction" || c["summary"] != "the story so far" || len(covers) != 2 || covers[0].(float64) != 0 || covers[1].(float64) != 1 {
		t.Fatalf("compaction %v", c)
	}
	if turns[4].Role != RoleNote {
		t.Fatalf("role %s", turns[4].Role)
	}
}

func TestPiSkipsDamagedLinesAndMasksSecrets(t *testing.T) {
	env := testEnv(t, nil)
	token := "ghp_" + strings.Repeat("A1b2C3d4E5", 3) + "A1b2C3"
	body := jsonl(t, piHeader("s1"),
		piMsg("a", "", "2026-03-01T10:00:01Z", "user", piText("push it"), nil),
		piAssistant("b", "a", "2026-03-01T10:00:02Z", map[string]any{"type": "toolCall", "id": "c1", "name": "bash", "arguments": map[string]any{"command": "git push"}}))
	body += "this is not json\n{\"type\":\"message\",\"id\":\n"
	body += jsonl(t, piMsg("c", "b", "2026-03-01T10:00:03Z", "toolResult", piText("remote: using "+token), piRec{"toolCallId": "c1", "toolName": "bash"}))
	path := piWrite(t, env, "s1")
	writeFile(t, path, body)

	p := piParser{}
	cand := Candidate{Harness: "pi", ID: "s1", Root: piStore(env), Path: path}
	idx, err := p.Index(env, &cand, true)
	if err != nil {
		t.Fatal(err)
	}
	if idx.Damaged != 1 || len(idx.Turns) != 3 {
		t.Fatalf("damaged %d turns %d", idx.Damaged, len(idx.Turns))
	}
	turns, last := readAll(t, piService(env), "pi", "s1", true, MaxPage)
	b, _ := json.Marshal(turns)
	if strings.Contains(string(b), token) || !strings.Contains(string(b), Mask) || last.Masked < 1 {
		t.Fatalf("masked %d: %s", last.Masked, b)
	}
}

func TestPiPeekReadsOnlyTheEnds(t *testing.T) {
	env := testEnv(t, nil)
	records := []any{piHeader("big"), piMsg("m0", "", "2026-03-01T10:00:01Z", "user", piText("the opening request"), nil)}
	parent := "m0"
	for i := 1; i <= 1500; i++ {
		id := "m" + strconv.Itoa(i)
		records = append(records, piMsg(id, parent, "2026-03-01T11:00:00Z", "user", piText(strings.Repeat("filler ", 10)), nil))
		parent = id
	}
	records = append(records, piEntry("session_info", "n", parent, "2026-03-01T12:00:00Z", piRec{"name": "Named at the end"}))
	path := piWrite(t, env, "big", records...)
	st, _ := os.Stat(path)
	if st.Size() < 200<<10 {
		t.Fatalf("fixture is only %d bytes", st.Size())
	}
	cand := Candidate{Harness: "pi", ID: "big", Root: piStore(env), Path: path}
	h, err := piParser{}.Peek(env, &cand)
	if err != nil {
		t.Fatal(err)
	}
	if h.Title != "Named at the end" || h.Cwd != "/home/someone/proj" || h.UpdatedAt.Format("15") != "12" || h.firstPrompt != "the opening request" {
		t.Fatalf("peek %+v", h)
	}
}

func TestPiRootsFollowTheEnvironmentAndLinksAreNotListed(t *testing.T) {
	env := testEnv(t, nil)
	if got := (piParser{}).Roots(env); len(got) != 1 || got[0] != piStore(env) {
		t.Fatalf("default roots %v", got)
	}
	env = testEnv(t, map[string]string{"PI_CODING_AGENT_DIR": "~/pi-home"})
	if got := (piParser{}).Roots(env); got[0] != filepath.Join(env.Home, "pi-home", "sessions") {
		t.Fatalf("dir roots %v", got)
	}
	env = testEnv(t, map[string]string{"PI_CODING_AGENT_DIR": "~/pi-home", "PI_CODING_AGENT_SESSION_DIR": "~/elsewhere"})
	if got := (piParser{}).Roots(env); got[0] != filepath.Join(env.Home, "elsewhere") {
		t.Fatalf("session dir roots %v", got)
	}
	writeFile(t, filepath.Join(env.Home, "elsewhere", "--p--", "t_in.jsonl"), jsonl(t, piHeader("in"),
		piMsg("a", "", "2026-03-01T10:00:01Z", "user", piText("inside"), nil)))
	outside := filepath.Join(env.Home, "outside")
	writeFile(t, filepath.Join(outside, "t_out.jsonl"), jsonl(t, piHeader("out"),
		piMsg("a", "", "2026-03-01T10:00:01Z", "user", piText("outside"), nil)))
	if err := os.Symlink(outside, filepath.Join(env.Home, "elsewhere", "--linked--")); err != nil {
		t.Skip("no symbolic links here")
	}
	r, err := piService(env).Scan(ScanRequest{Harness: "pi", Path: "/home/someone/proj"})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Here) != 1 || r.Here[0].ID != "in" {
		t.Fatalf("listed %+v", r.Here)
	}
}
