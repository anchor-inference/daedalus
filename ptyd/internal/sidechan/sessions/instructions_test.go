package sessions

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestClaudeInstructionsAreTheFilesItPutsInTheModelsPrompt(t *testing.T) {
	env := testEnv(t, nil)
	proj := filepath.Join(env.Home, "work", "proj")
	writeFile(t, filepath.Join(env.Home, ".claude", "CLAUDE.md"), "Answer in Russian.")
	writeFile(t, filepath.Join(env.Home, "work", "CLAUDE.md"), "Every repository here is public.")
	writeFile(t, filepath.Join(proj, "CLAUDE.md"), "Run the gates before a commit. token=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789")
	writeFile(t, filepath.Join(proj, "CLAUDE.local.md"), "The stand is work-sandbox.")
	store := filepath.Join(claudeStore(env), "-work-proj")
	writeFile(t, filepath.Join(store, "s1.jsonl"), jsonl(t, cUser("u1", "", "2026-03-01T10:00:00Z", "go on", map[string]any{"cwd": proj})))
	writeFile(t, filepath.Join(store, "memory", "MEMORY.md"), "- [Stands](stands.md)\n- [Delivery](delivery.md)\n")
	writeFile(t, filepath.Join(store, "memory", "delivery.md"), "Deliver by format-patch.")
	writeFile(t, filepath.Join(store, "memory", "stands.md"), "work-sandbox is the client stand.")
	writeFile(t, filepath.Join(store, "memory", "aside.md"), "Not linked, still a note.")
	writeFile(t, filepath.Join(env.Home, ".claude", ".credentials.json"), "{}")

	reply, err := claudeService(t, env).Instructions(InstructionsRequest{Harness: "claude", ID: "s1"})
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, f := range reply.Files {
		got = append(got, f.Kind+":"+strings.TrimPrefix(f.Path, NormPath(env.Home)))
	}
	want := "user:/.claude/CLAUDE.md project:/work/CLAUDE.md project:/work/proj/CLAUDE.md local:/work/proj/CLAUDE.local.md " +
		"memory_index:/.claude/projects/-work-proj/memory/MEMORY.md memory:/.claude/projects/-work-proj/memory/stands.md " +
		"memory:/.claude/projects/-work-proj/memory/delivery.md memory:/.claude/projects/-work-proj/memory/aside.md"
	if strings.Join(got, " ") != want {
		t.Fatalf("files\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	if reply.Cwd != proj {
		t.Fatalf("cwd %q", reply.Cwd)
	}
	project := reply.Files[2]
	if strings.Contains(project.Content, "abcdefghijklmnopqrstuvwxyz0123456789") || !strings.Contains(project.Content, "Run the gates") {
		t.Fatalf("the file is masked as a transcript is: %q", project.Content)
	}
}

func TestInstructionsAreBoundedAndNamedPastTheBound(t *testing.T) {
	env := testEnv(t, nil)
	proj := filepath.Join(env.Home, "proj")
	store := filepath.Join(claudeStore(env), "-proj")
	writeFile(t, filepath.Join(store, "s1.jsonl"), jsonl(t, cUser("u1", "", "2026-03-01T10:00:00Z", "go on", map[string]any{"cwd": proj})))
	writeFile(t, filepath.Join(store, "memory", "MEMORY.md"), strings.Repeat("i", InstructionFileMax+10))
	for _, name := range []string{"a.md", "b.md", "c.md", "d.md"} {
		writeFile(t, filepath.Join(store, "memory", name), strings.Repeat("n", InstructionFileMax-1))
	}
	reply, err := claudeService(t, env).Instructions(InstructionsRequest{Harness: "claude", ID: "s1"})
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	var omitted []string
	for _, f := range reply.Files {
		total += len(f.Content)
		if f.Omitted {
			omitted = append(omitted, filepath.Base(f.Path))
		}
	}
	if !reply.Files[0].Truncated || len(reply.Files[0].Content) != InstructionFileMax {
		t.Fatalf("a file past its bound is cut and says so: %d bytes, truncated=%v", len(reply.Files[0].Content), reply.Files[0].Truncated)
	}
	if total > InstructionsMax || len(omitted) == 0 || omitted[len(omitted)-1] != "d.md" {
		t.Fatalf("total %d, omitted %v", total, omitted)
	}
}

func TestCodexInstructionsAreItsAgentsFilesAndMemorySummary(t *testing.T) {
	env := testEnv(t, nil)
	proj := filepath.Join(env.Home, "proj")
	writeFile(t, filepath.Join(env.Home, ".codex", "AGENTS.md"), "Global rules.")
	writeFile(t, filepath.Join(proj, "AGENTS.md"), "Project rules.")
	writeFile(t, filepath.Join(env.Home, ".codex", "memories", "memory_summary.md"), "What Codex remembers.")
	sources := instructionSources(env, "codex", proj, "")
	var found []string
	for _, src := range sources {
		if f, ok := readInstruction(env, src, InstructionsMax); ok {
			found = append(found, f.Kind+":"+strings.TrimPrefix(f.Path, NormPath(env.Home)))
		}
	}
	if strings.Join(found, " ") != "user:/.codex/AGENTS.md project:/proj/AGENTS.md memory_index:/.codex/memories/memory_summary.md" {
		t.Fatalf("codex files %v", found)
	}
}
