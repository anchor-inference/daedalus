package rpc

import (
	"os"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/containment"
	"github.com/ascorblack/daedalus/ptyd/internal/sandbox"
	"github.com/ascorblack/daedalus/ptyd/internal/term"
)

type terminalPlanHandle struct{ scope containment.Scope }

func (h terminalPlanHandle) FD() int                    { return -1 }
func (h terminalPlanHandle) Scope() containment.Scope   { return h.scope }
func (h terminalPlanHandle) Limits() containment.Limits { return containment.Limits{} }
func (h terminalPlanHandle) Observe() (containment.Evidence, error) {
	return containment.Evidence{}, nil
}
func (h terminalPlanHandle) Kill(time.Duration) (containment.Evidence, error) {
	return containment.Evidence{}, nil
}
func (h terminalPlanHandle) Close() error { return nil }

func TestTerminalPlanDescriptorsNeedTheExactAttempt(t *testing.T) {
	scope := containment.Scope{AttemptID: "attempt-one", HostGeneration: "1", LaunchID: "launch-one"}
	plan := &sandbox.Plan{ExtraFiles: []*os.File{nil}}
	spec := term.Spec{LaunchID: scope.LaunchID, Containment: terminalPlanHandle{scope: scope}}
	if err := validateTerminalPlan(spec, plan, &scope); err != nil {
		t.Fatalf("exact attempt refused: %v", err)
	}
	for name, change := range map[string]func(*term.Spec, *containment.Scope){
		"no containment":       func(s *term.Spec, _ *containment.Scope) { s.Containment = nil },
		"no launch":            func(s *term.Spec, _ *containment.Scope) { s.LaunchID = "" },
		"different launch":     func(s *term.Spec, _ *containment.Scope) { s.LaunchID = "launch-two" },
		"different attempt":    func(_ *term.Spec, c *containment.Scope) { c.AttemptID = "attempt-two" },
		"different generation": func(_ *term.Spec, c *containment.Scope) { c.HostGeneration = "2" },
	} {
		t.Run(name, func(t *testing.T) {
			candidate, requested := spec, scope
			change(&candidate, &requested)
			if err := validateTerminalPlan(candidate, plan, &requested); err == nil {
				t.Fatal("descriptor-bearing plan lost its exact attempt binding")
			}
		})
	}
	if err := validateTerminalPlan(spec, plan, nil); err == nil {
		t.Fatal("a plan without a requested attempt was admitted")
	}
	if err := validateTerminalPlan(term.Spec{}, &sandbox.Plan{}, nil); err != nil {
		t.Fatalf("ordinary terminal changed: %v", err)
	}
}
