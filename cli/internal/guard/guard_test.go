package guard

import (
	"context"
	"errors"
	"testing"
)

func TestReadOnlyCommands(t *testing.T) {
	readOnly := []string{
		"ls -la",
		"cat README.md | head -n 20",
		"rg -n TODO src",
		"git status",
		"git -C sub log --oneline -5",
		"git diff HEAD~1 -- a.go",
		"find . -name '*.go' -type f",
		"grep -rn foo . 2>/dev/null | wc -l",
		"cd src && ls",
		"sed -n 1,20p file.txt",
		"echo $(git rev-parse HEAD)",
		"exec 2>&1\nls",
		// What the server's file tools send through the shell.
		`f='/w/a.txt'; if [ ! -e "$f" ]; then echo MISSING; exit 4; fi; size=$(wc -c < "$f" | tr -d ' '); tail -n +1 "$f" 2>/dev/null | head -n 201 2>/dev/null | cut -b 1-4000 2>/dev/null | head -c 100000`,
		`for f in /w/*.go; do [ -f "$f" ] && printf '%s\n' "$f"; done; true`,
		`if command -v rg >/dev/null 2>&1; then rg -n --no-heading -e x /w; else grep -rnE -e x /w | head -n 50; fi`,
		"tail -c +1 '/w/a.bin' 2>/dev/null | head -c 1000 | base64",
		"FOO=1 env",
		"timeout 5 ls",
		"bash -c 'ls -1'",
	}
	for _, c := range readOnly {
		if !ReadOnlyCommand(c) {
			t.Errorf("read-only, but refused: %q", c)
		}
	}
	writes := []string{
		"rm -rf build",
		"touch x",
		"echo hi > out.txt",
		"echo hi >> out.txt",
		"ls > files.txt",
		"sed -i s/a/b/ file",
		"find . -delete",
		`find . -exec rm {} \;`,
		"git commit -m x",
		"git branch new-branch",
		"git checkout main",
		"npm install",
		"cat a | tee b",
		"sort -o out in",
		"uniq in out",
		"echo `rm x`",
		"echo $(rm x)",
		"ls & rm x",
		"cat <<EOF\nx\nEOF",
		". ./script.sh",
		"python3 -c 'print(1)'",
		"awk '{print > \"x\"}' f",
		"env rm x",
		"timeout 5 rm x",
		"bash -c 'rm x'",
		"exec rm x",
		"xargs rm < list",
		"go env -w GOFLAGS=x",
		"'unterminated",
	}
	for _, c := range writes {
		if ReadOnlyCommand(c) {
			t.Errorf("writes, but passed as read-only: %q", c)
		}
	}
}

type fakePrompter struct {
	decision Decision
	asked    []Action
}

func (f *fakePrompter) Confirm(ctx context.Context, a Action) (Decision, error) {
	f.asked = append(f.asked, a)
	return f.decision, nil
}

func TestGuardLevels(t *testing.T) {
	ctx := context.Background()
	write := Action{Kind: Write, Method: "fs.write", Path: "/w/a"}
	touch := Action{Kind: Exec, Method: "exec.run", Command: "touch a"}
	list := Action{Kind: Exec, Method: "exec.run", Command: "ls"}
	read := Action{Kind: Read, Method: "fs.read", Path: "/w/a"}

	auto := New(Auto, nil)
	for _, a := range []Action{write, touch, list, read} {
		if err := auto.Check(ctx, a); err != nil {
			t.Errorf("auto refused %v: %v", a, err)
		}
	}

	plan := New(Plan, nil)
	for _, a := range []Action{write, touch} {
		err := plan.Check(ctx, a)
		var r *Refusal
		if !errors.As(err, &r) || r.Level != Plan || !errors.Is(err, ErrRefused) {
			t.Errorf("plan let %v through: %v", a, err)
		}
	}
	for _, a := range []Action{list, read} {
		if err := plan.Check(ctx, a); err != nil {
			t.Errorf("plan refused a read %v: %v", a, err)
		}
	}

	// Ask with nobody at the terminal refuses what it would ask about, and runs reads.
	alone := New(Ask, nil)
	if err := alone.Check(ctx, touch); err == nil {
		t.Error("ask without a prompter ran a command")
	}
	if err := alone.Check(ctx, list); err != nil {
		t.Errorf("ask refused a read-only command: %v", err)
	}

	p := &fakePrompter{decision: Decision{Allow: false, Reason: "use make"}}
	ask := New(Ask, p)
	err := ask.Check(ctx, touch)
	var r *Refusal
	if !errors.As(err, &r) || r.Reason != "the operator said no: use make" {
		t.Errorf("a refusal lost its reason: %v", err)
	}
	p.decision = Decision{Allow: true, Remember: true}
	if err := ask.Check(ctx, Action{Kind: Exec, Command: "npm test -- --watch"}); err != nil {
		t.Fatal(err)
	}
	asked := len(p.asked)
	if err := ask.Check(ctx, Action{Kind: Exec, Command: "npm test"}); err != nil || len(p.asked) != asked {
		t.Errorf("a remembered yes asked again (%d → %d): %v", asked, len(p.asked), err)
	}
	if err := ask.Check(ctx, Action{Kind: Exec, Command: "npm publish"}); err != nil || len(p.asked) != asked+1 {
		t.Errorf("a yes for npm test covered npm publish")
	}
}

func TestLevelCycleAndNames(t *testing.T) {
	if Ask.Next() != Auto || Auto.Next() != Plan || Plan.Next() != Ask {
		t.Error("Shift+Tab does not cycle ask → auto → plan → ask")
	}
	for _, s := range []string{"ask", "auto", "plan", "read-only"} {
		if _, err := ParseLevel(s); err != nil {
			t.Errorf("%s: %v", s, err)
		}
	}
	if _, err := ParseLevel("yolo"); err == nil {
		t.Error("an unknown level parsed")
	}
	if got := Similar(Action{Kind: Exec, Command: "npm test --watch"}); got != "npm test *" {
		t.Errorf("similar: %q", got)
	}
}
