package main

import "strings"

// fenceFailureKey names the sentence for an update whose data switch did not commit. The switch's
// own message (fenceOutcomeError) is English with the outcome's code in it, and on the page it was
// the whole explanation: "REFUSED", "FAIL_CLOSED" and a path mean something to the report's reader,
// not to someone who pressed Update. The sentence says what it means for them and what to do; the
// original stays in the log beside it. text is already lower case.
func fenceFailureKey(text string) string {
	if strings.Contains(text, "the switch of the data folder did not finish") {
		return "trouble.fence.unfinished"
	}
	// A full disk has a sentence of its own, and it is the more useful one — whether the disk filled
	// up during the copy or the space was found short before it.
	if !strings.Contains(text, "could not be switched safely") || strings.Contains(text, "no space left on device") || strings.Contains(text, "not enough free space") {
		return ""
	}
	switch {
	case strings.Contains(text, "("+strings.ToLower(fenceRefused)+")"):
		return "trouble.fence.refused"
	case strings.Contains(text, "("+strings.ToLower(fenceRolledBack)+")"):
		return "trouble.fence.rolledback"
	}
	return "trouble.fence.failclosed"
}
