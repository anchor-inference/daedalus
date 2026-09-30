package main

import (
	"strings"
	"testing"
)

// The switch's refusal reaches the page as a sentence in the operator's language, chosen by the
// outcome; the message it is recognised by is the one the update really returns.
func TestAFenceRefusalIsSaidInTheOperatorsLanguage(t *testing.T) {
	for outcome, want := range map[string]string{
		fenceRefused:    "trouble.fence.refused",
		fenceRolledBack: "trouble.fence.rolledback",
		fenceFailClosed: "trouble.fence.failclosed",
	} {
		err := fenceOutcomeError(fenceReport{Outcome: outcome, Reason: "writer activity", ReportPath: "/x/report.json",
			Entries: []fenceEntryReport{{Path: "state/daedalus.sqlite", Cause: "open for writing"}}})
		if got := FailureKey(err.Error()); got != want {
			t.Errorf("%s: %q gets %q, not %q", outcome, err, got, want)
		}
		for _, lang := range []Lang{LangEN, LangRU} {
			if line := Translate(lang, want); line == want || strings.Contains(line, outcome) {
				t.Errorf("%s: %s says %q", lang, want, line)
			}
		}
	}
	full := fenceOutcomeError(fenceReport{Outcome: fenceFailClosed, Reason: "copying: write: no space left on device"})
	if got := FailureKey(full.Error()); got != "trouble.disk" {
		t.Errorf("a full disk during the copy is %q, not the disk's sentence", got)
	}
	short := fenceOutcomeError(fenceReport{Outcome: fenceRefused, Reason: "not enough free space for the copy of the data folder: it needs 2.0 GiB"})
	if got := FailureKey(short.Error()); got != "trouble.disk" {
		t.Errorf("too little room for the copy is %q, not the disk's sentence", got)
	}
	// A switch a third party interfered with after the exchange: not "nothing was changed".
	unfinished := fenceOutcomeError(fenceReport{Outcome: fenceFailClosed, Live: "C", Reason: "someone else exchanged or renamed the trees"})
	if strings.Contains(unfinished.Error(), "nothing was changed") {
		t.Errorf("says nothing changed although the copy is live: %v", unfinished)
	}
	if got := FailureKey(unfinished.Error()); got != "trouble.fence.unfinished" {
		t.Errorf("an unfinished switch is %q", got)
	}
}
