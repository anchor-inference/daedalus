package sessions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// claudeRec is one synthetic Claude Code record.
type claudeRec map[string]any

func cUser(uuid, parent, at string, content any, extra ...map[string]any) claudeRec {
	r := claudeRec{"type": "user", "uuid": uuid, "parentUuid": nilIfEmpty(parent), "timestamp": at, "isSidechain": false,
		"cwd": "/home/someone/proj", "sessionId": "s1", "version": "2.1.0", "gitBranch": "main",
		"message": map[string]any{"role": "user", "content": content}}
	for _, e := range extra {
		for k, v := range e {
			r[k] = v
		}
	}
	return r
}

func cAssistant(uuid, parent, at, msgID string, block map[string]any) claudeRec {
	return claudeRec{"type": "assistant", "uuid": uuid, "parentUuid": nilIfEmpty(parent), "timestamp": at, "isSidechain": false,
		"cwd": "/home/someone/proj", "sessionId": "s1",
		"message": map[string]any{"id": msgID, "model": "claude-test-1", "role": "assistant", "content": []any{block},
			"usage": map[string]any{"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}}}
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func claudeService(t *testing.T, env *Env) *Service {
	return NewService(env, []Parser{claudeParser{}})
}

func claudeStore(env *Env) string { return filepath.Join(env.Home, ".claude", "projects") }

func TestClaudeMergesBlocksByMessageAndFollowsTheActiveBranch(t *testing.T) {
	env := testEnv(t, nil)
	recs := []any{
		claudeRec{"type": "permission-mode", "permissionMode": "default", "sessionId": "s1"},
		cUser("u1", "", "2026-03-01T10:00:00Z", "fix the failing test"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "thinking", "thinking": "look at it", "signature": "sig"}),
		cAssistant("a2", "a1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": map[string]any{"command": "pytest -q"}}),
		cUser("r1", "a2", "2026-03-01T10:00:02Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_1", "content": "1 failed", "is_error": true}}),
		// A branch the operator rewound away from: it stays in the file and must not be read.
		cAssistant("dead", "r1", "2026-03-01T10:00:03Z", "msg_dead", map[string]any{"type": "text", "text": "abandoned answer"}),
		cUser("u2", "r1", "2026-03-01T10:00:04Z", "try again, differently"),
		cAssistant("a3", "u2", "2026-03-01T10:00:05Z", "msg_2", map[string]any{"type": "text", "text": "done"}),
		claudeRec{"type": "ai-title", "aiTitle": "Fixing a test", "sessionId": "s1"},
	}
	writeFile(t, filepath.Join(claudeStore(env), "-home-someone-proj", "s1.jsonl"), jsonl(t, recs...))
	s := claudeService(t, env)
	turns, last := readAll(t, s, "claude", "s1", true, MaxPage)
	var got []string
	for _, tr := range turns {
		got = append(got, tr.Role+"["+kinds(tr)+"]")
	}
	want := "user[text] assistant[thinking,tool_call] user[tool_result] user[text] assistant[text]"
	if strings.Join(got, " ") != want {
		t.Fatalf("turns\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	if turns[1].ExtID != "a1" || turns[1].Model != "claude-test-1" || turns[1].Usage == nil || turns[1].Usage.Input != 10 {
		t.Fatalf("assistant turn %+v", turns[1])
	}
	if p := turns[1].Parts[1]; p.Name != "Bash" || p.CallID != "toolu_1" || string(p.Input) != `{"command":"pytest -q"}` || p.ToolKind != ToolNative {
		t.Fatalf("tool call %+v", p)
	}
	if p := turns[2].Parts[0]; !p.IsError || p.Output != "1 failed" || p.CallID != "toolu_1" {
		t.Fatalf("tool result %+v", p)
	}
	for i, tr := range turns {
		if tr.Seq != i {
			t.Fatalf("seq %d at %d", tr.Seq, i)
		}
		for _, p := range tr.Parts {
			if p.Text == "abandoned answer" {
				t.Fatal("the abandoned branch was read")
			}
		}
	}
	h := last.Header
	if h.Title != "Fixing a test" || h.Cwd != "/home/someone/proj" || h.Messages != 3 || h.Branch != "main" || h.Model != "claude-test-1" {
		t.Fatalf("header %+v", h)
	}
	if h.StartedAt.Format("15:04:05") != "10:00:00" || h.UpdatedAt.Format("15:04:05") != "10:00:05" {
		t.Fatalf("times %v %v", h.StartedAt, h.UpdatedAt)
	}
}

func TestClaudeTitlePrecedence(t *testing.T) {
	cases := []struct {
		name  string
		extra []any
		want  string
	}{
		{"custom title wins", []any{
			claudeRec{"type": "ai-title", "aiTitle": "ai"}, claudeRec{"type": "custom-title", "customTitle": "mine"},
			claudeRec{"type": "agent-name", "agentName": "agent"}, claudeRec{"type": "last-prompt", "lastPrompt": "last"}}, "mine"},
		{"the last custom title", []any{
			claudeRec{"type": "custom-title", "customTitle": "first"}, claudeRec{"type": "custom-title", "customTitle": "second"}}, "second"},
		{"ai title before the agent name", []any{
			claudeRec{"type": "agent-name", "agentName": "agent"}, claudeRec{"type": "ai-title", "aiTitle": "ai"}}, "ai"},
		{"agent name before the last prompt", []any{
			claudeRec{"type": "last-prompt", "lastPrompt": "last"}, claudeRec{"type": "agent-name", "agentName": "agent"}}, "agent"},
		{"the last prompt before the first request", []any{claudeRec{"type": "last-prompt", "lastPrompt": "last"}}, "last"},
		{"the first request, past the CLI's own records", nil, "the real request"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env := testEnv(t, nil)
			recs := []any{
				cUser("m0", "", "2026-03-01T10:00:00Z", "<command-name>/init</command-name>"),
				cUser("m1", "m0", "2026-03-01T10:00:00Z", "context the CLI added", map[string]any{"isMeta": true}),
				cUser("u1", "m1", "2026-03-01T10:00:01Z", "the real request\nsecond line"),
			}
			recs = append(recs, tc.extra...)
			writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), jsonl(t, recs...))
			s := claudeService(t, env)
			// Both the light header (the listing) and the full one (one pass) agree.
			r, err := s.Scan(ScanRequest{Harness: "claude", Path: "/home/someone/proj"})
			if err != nil {
				t.Fatal(err)
			}
			if len(r.Here) != 1 || r.Here[0].Title != tc.want {
				t.Fatalf("scan %+v", r.Here)
			}
			light, _ := s.lightPass(claudeParser{}, testNow.Add(ScanBudget))
			if light[0].header.Title != tc.want {
				t.Fatalf("light title %q", light[0].header.Title)
			}
		})
	}
}

func TestClaudeCompactionCrossesTheBoundaryAndCoversTheTurnsBefore(t *testing.T) {
	env := testEnv(t, nil)
	recs := []any{
		cUser("u1", "", "2026-03-01T10:00:00Z", "start"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "text", "text": "ok"}),
		claudeRec{"type": "system", "subtype": "compact_boundary", "uuid": "cb", "parentUuid": nil, "logicalParentUuid": "a1",
			"timestamp": "2026-03-01T10:00:02Z", "compactMetadata": map[string]any{"trigger": "auto", "preTokens": 1000}},
		cUser("cs", "cb", "2026-03-01T10:00:02Z", "Summary: the work so far", map[string]any{"isCompactSummary": true, "isVisibleInTranscriptOnly": true}),
		cUser("u2", "cs", "2026-03-01T10:00:03Z", "go on"),
		cAssistant("a2", "u2", "2026-03-01T10:00:04Z", "msg_2", map[string]any{"type": "text", "text": "continuing"}),
		claudeRec{"type": "system", "subtype": "compact_boundary", "uuid": "cb2", "parentUuid": nil, "logicalParentUuid": "a2",
			"timestamp": "2026-03-01T10:00:05Z", "compactMetadata": map[string]any{"trigger": "manual"}},
		cUser("cs2", "cb2", "2026-03-01T10:00:05Z", "Second summary", map[string]any{"isCompactSummary": true}),
	}
	writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), jsonl(t, recs...))
	turns, last := readAll(t, claudeService(t, env), "claude", "s1", true, MaxPage)
	if len(turns) != 6 {
		t.Fatalf("%d turns: %v", len(turns), turns)
	}
	c := turns[2].Parts[0]
	if turns[2].Role != RoleNote || c.Kind != KindCompaction || c.Summary != "Summary: the work so far" || !c.Auto || c.Covers == nil || *c.Covers != [2]int{0, 1} {
		t.Fatalf("first compaction %+v", turns[2])
	}
	c = turns[5].Parts[0]
	if c.Auto || c.Covers == nil || *c.Covers != [2]int{3, 4} || c.Summary != "Second summary" {
		t.Fatalf("second compaction %+v", turns[5])
	}
	if last.Header.Flags.Compacted != 2 || last.Header.Messages != 4 {
		t.Fatalf("header %+v", last.Header)
	}
	w := wire(t, c)
	if w["kind"] != "compaction" || w["auto"] != false || len(w) != 4 {
		t.Fatalf("wire %v", w)
	}
}

func TestClaudeSubagentsAreInlinedAfterTheCallThatStartedThem(t *testing.T) {
	env := testEnv(t, nil)
	dir := filepath.Join(claudeStore(env), "-p")
	writeFile(t, filepath.Join(dir, "s1.jsonl"), jsonl(t,
		cUser("u1", "", "2026-03-01T10:00:00Z", "research it"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "msg_1", map[string]any{"type": "tool_use", "id": "toolu_task", "name": "Agent", "input": map[string]any{"prompt": "look"}}),
		cUser("r1", "a1", "2026-03-01T10:00:09Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "toolu_task", "content": []any{map[string]any{"type": "text", "text": "found it"}}}}),
		cAssistant("a2", "r1", "2026-03-01T10:00:10Z", "msg_2", map[string]any{"type": "text", "text": "summary"}),
	))
	sub := filepath.Join(dir, "s1", "subagents")
	writeFile(t, filepath.Join(sub, "agent-abc.jsonl"), jsonl(t,
		cUser("x1", "", "2026-03-01T10:00:02Z", "look", map[string]any{"isSidechain": true, "agentId": "abc"}),
		cAssistant("x2", "x1", "2026-03-01T10:00:03Z", "msg_x", map[string]any{"type": "text", "text": "found it"}),
	))
	writeFile(t, filepath.Join(sub, "agent-abc.meta.json"), `{"agentType":"Explore","description":"look around","toolUseId":"toolu_task"}`)
	s := claudeService(t, env)
	turns, last := readAll(t, s, "claude", "s1", true, MaxPage)
	var got []string
	for _, tr := range turns {
		got = append(got, tr.Sidechain+":"+tr.Role)
	}
	if strings.Join(got, " ") != ":user :assistant :user abc:user abc:assistant :assistant" {
		t.Fatalf("order %v", got)
	}
	if m := turns[3].Parts[0]; m.MetaType != "sidechain" || m.Data.(map[string]any)["call_id"] != "toolu_task" || m.Data.(map[string]any)["agent_type"] != "Explore" {
		t.Fatalf("sidechain note %+v", m)
	}
	if last.Header.Flags.Sidechains != 1 {
		t.Fatalf("header %+v", last.Header)
	}
	main, _ := readAll(t, s, "claude", "s1", false, MaxPage)
	if len(main) != 4 {
		t.Fatalf("without sub-agents %d turns", len(main))
	}
	// The sub-agent's files are part of the session's size, and not a session of their own.
	r, _ := s.Scan(ScanRequest{Harness: "claude", Path: "/home/someone/proj"})
	if len(r.Here) != 1 || r.Here[0].Bytes <= 0 {
		t.Fatalf("scan %+v", r.Here)
	}
}

func TestClaudeNoiseMetaAttachmentsAndImages(t *testing.T) {
	env := testEnv(t, nil)
	png := "iVBORw0KGgo="
	writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), jsonl(t,
		cUser("u0", "", "2026-03-01T10:00:00Z", "<local-command-stdout>ok</local-command-stdout>"),
		claudeRec{"type": "attachment", "uuid": "at1", "parentUuid": "u0", "timestamp": "2026-03-01T10:00:00Z",
			"attachment": map[string]any{"type": "skill_listing"}, "rendered": []any{map[string]any{"content": "skills"}}},
		claudeRec{"type": "attachment", "uuid": "at2", "parentUuid": "at1", "timestamp": "2026-03-01T10:00:00Z",
			"attachment": map[string]any{"type": "date_change"}},
		cUser("u1", "at2", "2026-03-01T10:00:01Z", []any{
			map[string]any{"type": "text", "text": "what is in\n<pasted_content id=\"7a\">\npasted words\n</pasted_content id=\"7a\">\n"},
			map[string]any{"type": "image", "source": map[string]any{"type": "base64", "media_type": "image/png", "data": png}}}),
		claudeRec{"type": "system", "subtype": "turn_duration", "uuid": "td", "parentUuid": "u1", "timestamp": "2026-03-01T10:00:02Z"},
		claudeRec{"type": "system", "subtype": "local_command", "uuid": "lc", "parentUuid": "td", "timestamp": "2026-03-01T10:00:02Z", "content": "ran /cost"},
		cUser("u2", "lc", "2026-03-01T10:00:03Z", "[Request interrupted by user]"),
	))
	turns, last := readAll(t, claudeService(t, env), "claude", "s1", true, MaxPage)
	var got []string
	for _, tr := range turns {
		got = append(got, tr.Role+"["+kinds(tr)+"]")
	}
	want := "user[meta:cli] system_note[meta:attachment:skill_listing,meta:attachment:date_change] user[text,image] system_note[meta:system:local_command] user[meta:interrupted]"
	if strings.Join(got, " ") != want {
		t.Fatalf("turns\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	if txt := turns[2].Parts[0].Text; txt != "what is in\n\npasted words" {
		t.Fatalf("unpasted %q", txt)
	}
	img := wire(t, turns[2].Parts[1])
	if img["mime"] != "image/png" || img["data_b64"] != png || img["bytes"].(float64) <= 0 {
		t.Fatalf("image %v", img)
	}
	if last.Header.Messages != 1 || last.Header.Title != "what is in" {
		t.Fatalf("header %+v", last.Header)
	}
}

func TestClaudeReadsBackAPersistedToolOutputOnlyFromItsStore(t *testing.T) {
	env := testEnv(t, nil)
	dir := filepath.Join(claudeStore(env), "-p")
	inside := filepath.Join(dir, "s1", "tool-results", "out.txt")
	writeFile(t, inside, strings.Repeat("full output ", 10))
	outside := filepath.Join(env.Home, "elsewhere.txt")
	writeFile(t, outside, "not the store's")
	result := func(id, path string) claudeRec {
		return cUser("r"+id, "a"+id, "2026-03-01T10:00:02Z",
			[]any{map[string]any{"type": "tool_result", "tool_use_id": "t" + id, "content": "<persisted-output>preview</persisted-output>"}},
			map[string]any{"toolUseResult": map[string]any{"persistedOutputPath": path, "persistedOutputSize": 120}})
	}
	writeFile(t, filepath.Join(dir, "s1.jsonl"), jsonl(t,
		cUser("u1", "", "2026-03-01T10:00:00Z", "go"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "m1", map[string]any{"type": "tool_use", "id": "t1", "name": "Bash", "input": map[string]any{}}),
		result("1", inside),
		cAssistant("a2", "r1", "2026-03-01T10:00:03Z", "m2", map[string]any{"type": "tool_use", "id": "t2", "name": "Bash", "input": map[string]any{}}),
		result("2", outside),
	))
	turns, _ := readAll(t, claudeService(t, env), "claude", "s1", true, MaxPage)
	if out := turns[2].Parts[0].Output; out != strings.Repeat("full output ", 10) {
		t.Fatalf("persisted output %q", out)
	}
	if out := turns[4].Parts[0].Output; out != "<persisted-output>preview</persisted-output>" {
		t.Fatalf("a file outside the store was read: %q", out)
	}
}

func TestClaudeMasksSecretsAndCountsThem(t *testing.T) {
	env := testEnv(t, nil)
	token := "ghp_" + strings.Repeat("A1b2C3d4E5", 4)
	writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), jsonl(t,
		cUser("u1", "", "2026-03-01T10:00:00Z", "use "+token+" to push"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "m1", map[string]any{"type": "tool_use", "id": "t1", "name": "Bash",
			"input": map[string]any{"command": "curl -H 'Authorization: Bearer abcdefghijklmnop123456' x", "env": map[string]any{"api_key": "DEMO_CREDENTIAL_1234567890"}}}),
		cUser("r1", "a1", "2026-03-01T10:00:02Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "t1", "content": "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"}}),
	))
	s := claudeService(t, env)
	turns, last := readAll(t, s, "claude", "s1", true, MaxPage)
	b, _ := json.Marshal(turns)
	for _, secret := range []string{token, "abcdefghijklmnop123456", "DEMO_CREDENTIAL_1234567890", "sk-proj-abcdefghijklmnopqrstuvwxyz"} {
		if strings.Contains(string(b), secret) {
			t.Fatalf("%s crossed unmasked: %s", secret, b)
		}
	}
	if last.Masked < 1 {
		t.Fatalf("masked %d", last.Masked)
	}
	r, _ := s.Scan(ScanRequest{Harness: "claude", Path: "/home/someone/proj"})
	if strings.Contains(r.Here[0].Title, token) || !strings.Contains(r.Here[0].Title, Mask) {
		t.Fatalf("title %q", r.Here[0].Title)
	}
}

func TestClaudePagesAHugeOutputAndSkipsDamagedLines(t *testing.T) {
	env := testEnv(t, nil)
	big := strings.Repeat("x", 300<<10)
	body := jsonl(t,
		cUser("u1", "", "2026-03-01T10:00:00Z", "go"),
		cAssistant("a1", "u1", "2026-03-01T10:00:01Z", "m1", map[string]any{"type": "tool_use", "id": "t1", "name": "Read", "input": map[string]any{}}),
	) + "{not json at all\n" + jsonl(t,
		cUser("r1", "a1", "2026-03-01T10:00:02Z", []any{map[string]any{"type": "tool_result", "tool_use_id": "t1", "content": big}}),
	)
	for i := 0; i < 40; i++ {
		body += jsonl(t, cAssistant("b"+itoa(int64(i)), map[bool]string{true: "r1", false: "b" + itoa(int64(i-1))}[i == 0],
			"2026-03-01T10:00:03Z", "mb"+itoa(int64(i)), map[string]any{"type": "text", "text": strings.Repeat("y", 1000)}))
	}
	// A live session's last record, cut mid-write: left for a later read.
	body += `{"type":"user","uuid":"cut","parentUuid":"b39","message":{"role":"user","content":"half`
	writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), body)
	s := claudeService(t, env)
	turns, last := readAll(t, s, "claude", "s1", true, MinPage)
	if len(turns) != 43 {
		t.Fatalf("%d turns", len(turns))
	}
	if p := turns[2].Parts[0]; !p.Truncated || len(p.Output) > MinPage || !strings.HasSuffix(p.Output, cutMark) {
		t.Fatalf("the big output was not cut: %d bytes, truncated %v", len(p.Output), p.Truncated)
	}
	full := int64(len(body)) - int64(len(`{"type":"user","uuid":"cut","parentUuid":"b39","message":{"role":"user","content":"half`))
	if last.Offset != full {
		t.Fatalf("offset %d, want %d", last.Offset, full)
	}
	// Each page fits the frame it was asked for.
	r, err := s.Read(ReadRequest{Harness: "claude", ID: "s1", From: 3, MaxBytes: MinPage})
	if err != nil {
		t.Fatal(err)
	}
	if b, _ := json.Marshal(r.(ReadReply).Turns); len(b) > MinPage {
		t.Fatalf("page of %d bytes", len(b))
	}
}

func TestClaudeScanGroupsByTheRecordedFolder(t *testing.T) {
	env := testEnv(t, nil)
	store := claudeStore(env)
	session := func(dir, id, cwd, at string) {
		writeFile(t, filepath.Join(store, dir, id+".jsonl"), jsonl(t,
			claudeRec{"type": "user", "uuid": "u", "timestamp": at, "cwd": cwd, "message": map[string]any{"role": "user", "content": "hi " + id}}))
	}
	// The folder names lie (the encoding is lossy); the records say where each session ran.
	session("-home-someone-a-b", "s1", "/home/someone/a-b", "2026-03-01T10:00:00Z")
	session("-home-someone-a-b", "s2", "/home/someone/a/b", "2026-03-02T10:00:00Z")
	session("-home-someone-a", "s3", "/home/someone/a", "2026-03-03T10:00:00Z")
	session("-home-someone-c", "s4", "/home/someone/c/", "2026-03-04T09:00:00Z")
	s := claudeService(t, env)
	all, err := s.Scan(ScanRequest{Harness: "claude"})
	if err != nil {
		t.Fatal(err)
	}
	var folders []string
	for _, f := range all.Folders {
		folders = append(folders, f.Path)
	}
	if strings.Join(folders, " ") != "/home/someone/c /home/someone/a /home/someone/a/b /home/someone/a-b" {
		t.Fatalf("folders %v", folders)
	}
	r, _ := s.Scan(ScanRequest{Harness: "claude", Path: "/home/someone"})
	if len(r.Here) != 0 || len(r.Children) != 3 {
		t.Fatalf("children %+v here %+v", r.Children, r.Here)
	}
	if c := r.Children[0]; c.Name != "a" || c.Sessions != 2 || c.Path != "/home/someone/a" || c.Latest.Format("01-02") != "03-03" {
		t.Fatalf("child %+v", c)
	}
	r, _ = s.Scan(ScanRequest{Harness: "claude", Path: "/home/someone/a"})
	if len(r.Here) != 1 || r.Here[0].ID != "s3" || len(r.Children) != 1 || r.Children[0].Name != "b" {
		t.Fatalf("a: %+v", r)
	}
	q, _ := s.Scan(ScanRequest{Harness: "claude", Query: "HI S2"})
	if len(q.Here) != 1 || q.Here[0].ID != "s2" {
		t.Fatalf("query %+v", q.Here)
	}
	// Paging the folder's sessions by cursor.
	r, _ = s.Scan(ScanRequest{Harness: "claude", Query: "hi", Limit: 2})
	if len(r.Here) != 2 || !r.Truncated || r.Cursor != "2" || r.Here[0].ID != "s4" {
		t.Fatalf("page %+v", r)
	}
	r, _ = s.Scan(ScanRequest{Harness: "claude", Query: "hi", Limit: 2, Cursor: r.Cursor})
	if len(r.Here) != 2 || r.Truncated {
		t.Fatalf("second page %+v", r)
	}
}

func TestClaudePathsFoldTheirCaseOnWindowsAndMac(t *testing.T) {
	for _, goos := range []string{"windows", "darwin", "linux"} {
		env := testEnv(t, nil)
		env.GOOS = goos
		writeFile(t, filepath.Join(claudeStore(env), "C--Users-Someone-Proj", "s1.jsonl"), jsonl(t,
			claudeRec{"type": "user", "uuid": "u", "timestamp": "2026-03-01T10:00:00Z", "cwd": `c:\Users\Someone\Proj\`, "message": map[string]any{"role": "user", "content": "hi"}}))
		r, err := NewService(env, []Parser{claudeParser{}}).Scan(ScanRequest{Harness: "claude", Path: "C:/users/someone/proj"})
		if err != nil {
			t.Fatal(err)
		}
		want := 1
		if goos == "linux" {
			want = 0
		}
		if len(r.Here) != want {
			t.Fatalf("%s: %d sessions", goos, len(r.Here))
		}
		if want == 1 && r.Here[0].Cwd != "C:/Users/Someone/Proj" {
			t.Fatalf("%s: cwd %q", goos, r.Here[0].Cwd)
		}
	}
}

func TestClaudeStoreFollowsTheConfigDirAndRefusesLinksOut(t *testing.T) {
	env := testEnv(t, map[string]string{"CLAUDE_CONFIG_DIR": "~/alt-claude"})
	store := filepath.Join(env.Home, "alt-claude", "projects")
	writeFile(t, filepath.Join(store, "-p", "s1.jsonl"), jsonl(t, cUser("u1", "", "2026-03-01T10:00:00Z", "hi")))
	// A link to a file outside the store, and a link to a folder outside: neither is read.
	outside := filepath.Join(env.Home, "outside")
	writeFile(t, filepath.Join(outside, "x.jsonl"), jsonl(t, cUser("u1", "", "2026-03-01T10:00:00Z", "outside")))
	if err := os.Symlink(filepath.Join(outside, "x.jsonl"), filepath.Join(store, "-p", "linked.jsonl")); err != nil {
		t.Skip("no symbolic links here")
	}
	if err := os.Symlink(outside, filepath.Join(store, "-linked")); err != nil {
		t.Fatal(err)
	}
	// A credentials file that happens to sit where a session could.
	writeFile(t, filepath.Join(env.Home, ".claude", ".credentials.json"), `{"token":"x"}`)
	s := NewService(env, []Parser{claudeParser{}})
	h := s.Harnesses()
	if len(h) != 1 || !h[0].Found || h[0].Root != "~/alt-claude/projects" || h[0].Sessions != 1 || h[0].Folders != 1 || h[0].Version != "2.1.0" {
		t.Fatalf("harnesses %+v", h)
	}
	if _, err := s.Read(ReadRequest{Harness: "claude", ID: "linked"}); err == nil {
		t.Fatal("a linked file was read")
	}
	if _, _, err := env.open(store, filepath.Join(env.Home, ".claude", ".credentials.json")); err == nil {
		t.Fatal("the credentials were opened")
	}
	if _, _, err := env.open(env.Home, filepath.Join(env.Home, ".claude", ".credentials.json")); err == nil {
		t.Fatal("the deny list was not applied")
	}
}

func TestReadRefusesBadRequests(t *testing.T) {
	env := testEnv(t, nil)
	s := claudeService(t, env)
	for _, req := range []ReadRequest{{Harness: "nope", ID: "x"}, {Harness: "claude", ID: ""}, {Harness: "claude", ID: "x", MaxBytes: MaxPage + 1},
		{Harness: "claude", ID: "x", From: -1}} {
		if _, err := s.Read(req); err == nil {
			t.Fatalf("%+v accepted", req)
		}
	}
	if _, err := s.Scan(ScanRequest{Harness: "claude", Cursor: "x"}); err == nil {
		t.Fatal("a bad cursor was accepted")
	}
}

func TestRawPagesWholeRecordsMasked(t *testing.T) {
	env := testEnv(t, nil)
	token := "ghp_" + strings.Repeat("Z9y8X7w6V5", 4)
	var recs []any
	for i := 0; i < 30; i++ {
		recs = append(recs, cUser("u"+itoa(int64(i)), "", "2026-03-01T10:00:00Z", strings.Repeat("w", 1000)+" "+token))
	}
	body := jsonl(t, recs...)
	writeFile(t, filepath.Join(claudeStore(env), "-p", "s1.jsonl"), body)
	s := claudeService(t, env)
	var got []byte
	masked := 0
	for from := int64(0); ; {
		r, err := s.Read(ReadRequest{Harness: "claude", ID: "s1", Raw: true, From: from, MaxBytes: MinPage})
		if err != nil {
			t.Fatal(err)
		}
		raw := r.(RawReply)
		if len(raw.Files) != 1 || raw.Files[0].Name != "s1.jsonl" {
			t.Fatalf("files %+v", raw.Files)
		}
		got = append(got, raw.Data...)
		masked += raw.Masked
		if raw.Done {
			break
		}
		from = raw.Next
	}
	if masked != 30 || strings.Contains(string(got), token) || string(got) != strings.ReplaceAll(body, token, Mask) {
		t.Fatalf("raw masked %d", masked)
	}
}

func TestLiveNeedsARecentRecord(t *testing.T) {
	env := testEnv(t, nil)
	writeFile(t, filepath.Join(claudeStore(env), "-p", "old.jsonl"), jsonl(t, cUser("u1", "", "2026-03-04T11:00:00Z", "old")))
	writeFile(t, filepath.Join(claudeStore(env), "-p", "new.jsonl"), jsonl(t, cUser("u1", "", "2026-03-04T11:59:30Z", "new")))
	r, _ := NewService(env, []Parser{claudeParser{}}).Scan(ScanRequest{Harness: "claude", Path: "/home/someone/proj"})
	live := map[string]bool{}
	for _, h := range r.Here {
		live[h.ID] = h.Flags.Live
	}
	// No process table in tests: the time decides.
	if live["old"] || !live["new"] {
		t.Fatalf("live %v", live)
	}
}

func TestTheWireShapes(t *testing.T) {
	h := wire(t, Header{V: 1, Harness: "claude", ID: "x"})
	for _, k := range []string{"v", "harness", "id", "cwd", "title", "started_at", "updated_at", "messages", "bytes", "branch", "model", "flags", "source"} {
		if _, ok := h[k]; !ok {
			t.Fatalf("header lacks %s: %v", k, h)
		}
	}
	if h["started_at"] != nil || len(h) != 13 {
		t.Fatalf("header %v", h)
	}
	call := wire(t, ToolCall("c1", "mcp__srv__do", json.RawMessage(`{"a":1}`), ToolMCP, "srv"))
	if call["kind"] != "tool_call" || call["tool_kind"] != "mcp" || call["server"] != "srv" || call["input"].(map[string]any)["a"] != 1.0 {
		t.Fatalf("tool call %v", call)
	}
	res := wire(t, ToolResult("c1", "out", true))
	if res["is_error"] != true || res["truncated"] != false || len(res["images"].([]any)) != 0 || len(res) != 6 {
		t.Fatalf("tool result %v", res)
	}
	th := wire(t, Thinking("", true))
	if th["encrypted"] != true || len(th) != 3 {
		t.Fatalf("thinking %v", th)
	}
	m := wire(t, Meta("x", nil))
	if m["kind"] != "meta" || m["type"] != "x" {
		t.Fatalf("meta %v", m)
	}
	turn := wire(t, Turn{Parts: []Part{}})
	for _, k := range []string{"seq", "ext_id", "parent", "role", "at", "sidechain", "model", "parts"} {
		if _, ok := turn[k]; !ok {
			t.Fatalf("turn lacks %s", k)
		}
	}
}
