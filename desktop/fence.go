package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// A fenced data switch copies the data folder, swaps the copy in with one atomic exchange and keeps
// the previous tree aside, and it does all of that while every writer of the folder is shut out by
// the kernel rather than merely looked for. A hash compared before and after proves nothing about a
// process that writes through a shared mapping one instant later; a read lease on every file does:
// the kernel refuses the lease while anyone holds the file writable, and blocks any new writer
// until the lease holder has decided. Directory and metadata changes, which leases do not cover,
// are caught by inotify on every directory. Where that fence cannot be built — another platform,
// another filesystem, files of another user, a container, root — the switch refuses before it
// changes anything.
//
// It is the protection of every update and upgrade where it can be had (protect.go). The copy is on
// the disk before it becomes live and the exchange before anything is decided on it: a switch that
// commits survives a power cut the moment after. The code below is the engine and its report.

// The outcome of one switch. Exactly one of these ends every attempt, and each has its own exit code
// so that a script can tell them apart without parsing the message.
const (
	fenceCommitted  = "COMMITTED"   // the copy is live at data; the previous tree is retained
	fenceRefused    = "REFUSED"     // a writer was found before the exchange; nothing changed
	fenceRolledBack = "ROLLED_BACK" // a writer was found after the exchange; the previous tree is live again
	fenceFailClosed = "FAIL_CLOSED" // the fence cannot be trusted here; nothing changed, or an operator must look
)

// fenceStatusRemoving marks a record whose tree is being removed: it is in the trash from the first
// unlink on, and a removal that was cut off goes on from there.
const fenceStatusRemoving = "removing"

// The outcome of removing a retained tree.
const (
	fenceDeleted      = "DELETED"
	fenceRetainedGC   = "RETAINED"
	fenceLostPossible = "LOST_POSSIBLE" // something may have written into a tree while it was being removed
)

func fenceExitCode(outcome string) int {
	switch outcome {
	case fenceCommitted:
		return 0
	case fenceRefused:
		return 3
	case fenceRolledBack:
		return 4
	}
	return 5
}

// fenceReport is written, with fsync, to <control>/reports/<op>.json before anything is shown to
// the operator. It is the record of truth: the message on screen is derived from it.
type fenceReport struct {
	Op                string             `json:"op"`
	Data              string             `json:"data"`
	Outcome           string             `json:"outcome"`
	ExitCode          int                `json:"exit_code"`
	Reason            string             `json:"reason"`
	Entries           []fenceEntryReport `json:"entries"`
	Retained          []fenceRetained    `json:"retained"`
	LateWritePossible []fenceLateWrite   `json:"late_write_possible"`
	Overflow          bool               `json:"overflow"`
	Unscanned         []fenceUnscanned   `json:"unscanned"`
	Limits            []string           `json:"limits"`
	GC                []fenceGCReport    `json:"gc,omitempty"`
	LeasesTaken       int                `json:"leases_taken"`
	Started           time.Time          `json:"started"`
	Finished          time.Time          `json:"finished"`
	DurationMS        int64              `json:"duration_ms"`
	// ReportPath is where the report was written; empty when the control folder could not hold it,
	// in which case the report went to standard error instead.
	ReportPath string `json:"-"`

	// Live is what the switch left at data: P (the folder as it was), C (the copy), or what a switch
	// that a third party interfered with found there. An outcome other than COMMITTED does not by
	// itself mean nothing changed; this does.
	Live string `json:"live,omitempty"`
}

type fenceEntryReport struct {
	Tree  string `json:"tree"`
	Path  string `json:"path"`
	Cause string `json:"cause"`
	PID   int    `json:"pid,omitempty"`
	TID   int    `json:"tid,omitempty"`
}

type fenceRetained struct {
	Path   string `json:"path"`
	Kind   string `json:"kind"` // pre, rejected, slot
	GC     string `json:"gc"`   // auto: may be removed under a fresh fence; manual: only on the operator's word
	Reason string `json:"reason,omitempty"`
}

type fenceLateWrite struct {
	Tree  string   `json:"tree"` // pre or live
	Paths []string `json:"paths"`
}

type fenceUnscanned struct {
	PID    int    `json:"pid"`
	Comm   string `json:"comm"`
	Reason string `json:"reason"`
}

type fenceGCReport struct {
	Name      string             `json:"name"`
	Outcome   string             `json:"outcome"`
	Reason    string             `json:"reason,omitempty"`
	Entries   []fenceEntryReport `json:"entries,omitempty"`
	Overflow  bool               `json:"overflow"`
	Deleted   int                `json:"deleted"`
	Unscanned []fenceUnscanned   `json:"unscanned,omitempty"`
	Limits    []string           `json:"limits,omitempty"`
	At        time.Time          `json:"at"`
}

// fenceRetainedMeta sits beside every retained tree as <name>.json. The manifest is the one recorded
// under the fence; removal compares the tree with it in full and keeps the tree on any difference.
// Status is removing while the tree is on its way through the trash, and LOST_POSSIBLE once a
// removal went wrong.
type fenceRetainedMeta struct {
	Name              string        `json:"name"`
	Kind              string        `json:"kind"`
	Op                string        `json:"op"`
	GC                string        `json:"gc"`
	Reason            string        `json:"reason,omitempty"`
	LateWritePossible []string      `json:"late_write_possible,omitempty"`
	Status            string        `json:"status,omitempty"`
	Created           time.Time     `json:"created"`
	Manifest          fenceManifest `json:"manifest"`
}

// fenceManifest is everything compared about a tree, keyed by path relative to its root. ctime and
// atime are deliberately absent: the exchange itself changes a root's ctime, and a reader changes
// atime, so neither can decide anything.
type fenceManifest map[string]fenceEntry

type fenceEntry struct {
	Kind    string            `json:"kind"` // dir, file, symlink
	Mode    uint32            `json:"mode"`
	UID     uint32            `json:"uid"`
	GID     uint32            `json:"gid"`
	Size    int64             `json:"size"`
	Nlink   uint64            `json:"nlink"`
	MtimeNS int64             `json:"mtime_ns,omitempty"`
	SHA256  string            `json:"sha256,omitempty"`
	Target  string            `json:"target,omitempty"`
	Xattrs  map[string]string `json:"xattrs,omitempty"`
	Flags   uint32            `json:"flags"`
}

// fenceManifestDiff names every path whose entry differs, in a stable order.
func fenceManifestDiff(want, got fenceManifest) []string {
	var diffs []string
	for path, a := range want {
		b, ok := got[path]
		if !ok {
			diffs = append(diffs, "missing: "+path)
			continue
		}
		if !fenceEntryEqual(a, b) {
			diffs = append(diffs, "changed: "+path)
		}
	}
	for path := range got {
		if _, ok := want[path]; !ok {
			diffs = append(diffs, "added: "+path)
		}
	}
	sort.Strings(diffs)
	return diffs
}

// fenceEntryEqual compares what an entry holds. A file's link count counts only where the recorded
// one is 1: a file that had a second name elsewhere when it was recorded — uv links a venv's files
// out of its cache, esbuild links its own binary — is copied as a file of its own, and the other name
// may come and go without the file changing; a new second name on a file that had none is a change.
func fenceEntryEqual(a, b fenceEntry) bool {
	links := a.Nlink != b.Nlink && (a.Kind != "file" || a.Nlink == 1)
	if a.Kind != b.Kind || a.Mode != b.Mode || a.UID != b.UID || a.GID != b.GID || a.Size != b.Size ||
		links || a.MtimeNS != b.MtimeNS || a.SHA256 != b.SHA256 || a.Target != b.Target ||
		a.Flags != b.Flags || len(a.Xattrs) != len(b.Xattrs) {
		return false
	}
	for name, value := range a.Xattrs {
		if b.Xattrs[name] != value {
			return false
		}
	}
	return true
}

// fenceControlFolder is the folder beside the data folder that holds every data folder's control
// folder.
const fenceControlFolder = ".daedalus-update"

// fenceControlName is the control folder's path relative to the data folder's parent: one per data
// folder, since two may share a parent.
func fenceControlName(data string) string {
	return filepath.Join(fenceControlFolder, filepath.Base(filepath.Clean(data)))
}

// fenceControlPath is the control folder beside data: same filesystem, so trees move into it with a
// rename; outside data, so nothing the updater keeps there is ever part of what it fences. v0.12
// launchers know nothing under the data folder's parent and never touch it.
func fenceControlPath(data string) string {
	return filepath.Join(filepath.Dir(filepath.Clean(data)), fenceControlName(data))
}

// fenceJournal is written, with fsync, before and after every step that changes where a tree is.
// It names both trees by inode, so that after a crash it can be told for certain which one is at
// data and which at the slot — a name alone could lead anywhere by then.
type fenceJournal struct {
	Op    string `json:"op"`
	Phase string `json:"phase"`
	Data  string `json:"data"`
	Slot  string `json:"slot,omitempty"`
	// SlotRetained: the second tree is a kept copy inside the control folder's retained, not a slot
	// beside data (a restore).
	SlotRetained bool      `json:"slot_retained,omitempty"`
	P            [2]uint64 `json:"p,omitempty"`
	C            [2]uint64 `json:"c,omitempty"`
	Updated      time.Time `json:"updated"`
}

// fenceJournalFinished: done is the switch's own end; resolved is the operator's, through
// `update resolve`.
func fenceJournalFinished(phase string) bool { return phase == "done" || phase == "resolved" }

// fenceItem is one thing the operator should know about the data folder's switches. The kinds are
// what the launcher's page translates; the command line prints them in English.
type fenceItem struct {
	Kind   string `json:"kind"` // retained, unrecorded, late, lost, unfinished, unscanned, trash, stray, upgrade
	Path   string `json:"path"`
	Detail string `json:"detail,omitempty"`
}

type fenceSummary struct {
	Items []fenceItem `json:"items"`
	// Trouble is set when a write may have been lost, may have landed outside the live data, or a
	// switch did not finish: `update status` then exits non-zero.
	Trouble bool `json:"trouble"`
}

func readJSON(path string, into any) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(body, into)
}

// fenceSummarize reads the control folder: every retained tree with its reason, every possible
// late write or loss, every switch that did not finish, and how many processes the newest switch
// could not inspect.
func fenceSummarize(control string) fenceSummary {
	var sum fenceSummary
	add := func(item fenceItem, trouble bool) {
		for _, have := range sum.Items {
			if have == item {
				return
			}
		}
		sum.Items = append(sum.Items, item)
		sum.Trouble = sum.Trouble || trouble
	}
	// An update or upgrade that neither finished nor was rolled back comes first: its remedy is
	// `upgrade --rollback`, not `update resolve`, and the page and the command line both say so.
	var upgrade Journal
	if err := readJSON(filepath.Join(control, "upgrade.json"), &upgrade); err == nil && upgrade.unresolved() {
		kind := upgrade.Kind
		if kind == "" {
			kind = kindUpgrade
		}
		add(fenceItem{Kind: "upgrade", Path: kind, Detail: upgrade.Stage}, true)
	}
	journals, _ := filepath.Glob(filepath.Join(control, "journal", "*.json"))
	sort.Strings(journals)
	for _, path := range journals {
		var j fenceJournal
		if err := readJSON(path, &j); err != nil {
			add(fenceItem{Kind: "unfinished", Path: path, Detail: "the journal cannot be read"}, true)
			continue
		}
		if !fenceJournalFinished(j.Phase) {
			add(fenceItem{Kind: "unfinished", Path: j.Op, Detail: j.Phase}, true)
		}
	}
	metas, _ := filepath.Glob(filepath.Join(control, "retained", "*.json"))
	sort.Strings(metas)
	for _, path := range metas {
		tree := strings.TrimSuffix(path, ".json")
		var meta fenceRetainedMeta
		if err := readJSON(path, &meta); err != nil {
			add(fenceItem{Kind: "unrecorded", Path: tree, Detail: "its record cannot be read"}, false)
			continue
		}
		detail := fmt.Sprintf("%s from %s, removal %s", meta.Kind, meta.Op, meta.GC)
		if meta.Reason != "" {
			detail += " — " + meta.Reason
		}
		if len(meta.Manifest) == 0 {
			add(fenceItem{Kind: "unrecorded", Path: tree, Detail: detail}, false)
		} else {
			add(fenceItem{Kind: "retained", Path: tree, Detail: detail}, false)
		}
		if len(meta.LateWritePossible) > 0 {
			add(fenceItem{Kind: "late", Path: tree, Detail: strings.Join(meta.LateWritePossible, ", ")}, true)
		}
		if meta.Status == fenceLostPossible {
			add(fenceItem{Kind: "lost", Path: tree, Detail: meta.Reason}, true)
		}
	}
	// Every tree the control folder holds is named, recorded or not: a copy nobody lists is a copy
	// nobody removes, and each is the size of the data folder.
	if entries, err := os.ReadDir(filepath.Join(control, "retained")); err == nil {
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			tree := filepath.Join(control, "retained", entry.Name())
			if _, err := os.Stat(tree + ".json"); err != nil {
				add(fenceItem{Kind: "unrecorded", Path: tree, Detail: "no record of what it holds"}, false)
			}
		}
	}
	if entries, err := os.ReadDir(filepath.Join(control, "trash")); err == nil {
		for _, entry := range entries {
			var meta fenceRetainedMeta
			detail := "left in the trash; remove it by hand once you no longer need it"
			if readJSON(filepath.Join(control, "retained", entry.Name()+".json"), &meta) == nil && meta.Status == fenceStatusRemoving {
				detail = "a removal was cut off; the next update goes on with it"
			}
			add(fenceItem{Kind: "trash", Path: filepath.Join(control, "trash", entry.Name()), Detail: detail}, false)
		}
	}
	dataName := filepath.Base(control)
	parent := filepath.Dir(filepath.Dir(control))
	if strays, _ := filepath.Glob(filepath.Join(parent, "."+dataName+"-slot-*")); len(strays) > 0 {
		sort.Strings(strays)
		for _, stray := range strays {
			add(fenceItem{Kind: "stray", Path: stray, Detail: "a copy a switch left beside the data folder"}, false)
		}
	}
	reports, _ := filepath.Glob(filepath.Join(control, "reports", "*.json"))
	sort.Strings(reports)
	var newest *fenceReport
	for _, path := range reports {
		base := filepath.Base(path)
		if strings.HasPrefix(base, "gc-") {
			var gc fenceGCReport
			if readJSON(path, &gc) == nil && gc.Outcome == fenceLostPossible {
				add(fenceItem{Kind: "lost", Path: filepath.Join(control, "retained", gc.Name), Detail: gc.Reason}, true)
			}
			continue
		}
		if strings.HasSuffix(base, "-resolved.json") {
			continue
		}
		var report fenceReport
		if readJSON(path, &report) != nil || report.Op == "" {
			continue
		}
		for _, late := range report.LateWritePossible {
			if late.Tree == "live" {
				add(fenceItem{Kind: "late", Path: report.Data, Detail: strings.Join(late.Paths, ", ")}, true)
			}
		}
		for _, gc := range report.GC {
			if gc.Outcome == fenceLostPossible {
				add(fenceItem{Kind: "lost", Path: filepath.Join(control, "retained", gc.Name), Detail: gc.Reason}, true)
			}
		}
		if newest == nil || report.Started.After(newest.Started) {
			r := report
			newest = &r
		}
	}
	if newest != nil && len(newest.Unscanned) > 0 {
		var names []string
		for _, u := range newest.Unscanned {
			names = append(names, fmt.Sprintf("%d %s", u.PID, u.Comm))
		}
		add(fenceItem{Kind: "unscanned", Path: fmt.Sprint(len(newest.Unscanned)), Detail: strings.Join(names, ", ")}, false)
	}
	return sum
}

var fenceItemText = map[string]string{
	"upgrade":    "the last %s did not finish (stage %s): `daedalus-desktop upgrade --rollback` puts everything back as it was before it",
	"retained":   "kept %s: %s",
	"unrecorded": "kept %s without a record of its contents (%s): remove it by hand once you no longer need it",
	"late":       "a late write may be in %s: %s",
	"lost":       "LOST_POSSIBLE: a write may have been lost while %s was being removed: %s",
	"unfinished": "a switch did not finish (%s, stopped at %s): `daedalus-desktop update resolve` says what is where",
	"trash":      "%s: %s",
	"stray":      "%s: %s; `daedalus-desktop update resolve` says whose it is, and it may be removed by hand once no switch is unfinished",
	"unscanned":  "%s processes could not be inspected during the last switch: %s",
}

// fenceStatus is `daedalus-desktop update status`: the summary in plain lines, and a non-zero code
// whenever a write may have been lost, may have landed outside the live data, or a switch or an
// update did not finish. The processes the last switch could not inspect are counted, and listed
// only when asked (verbose): on a desktop they are the other programs of the session, every time.
func fenceStatus(control string, verbose bool) (string, int) {
	sum := fenceSummarize(control)
	var b strings.Builder
	for _, item := range sum.Items {
		if item.Kind == "unscanned" && !verbose {
			fmt.Fprintf(&b, "%s processes could not be inspected during the last switch (`update status -v` lists them)\n", item.Path)
			continue
		}
		fmt.Fprintf(&b, fenceItemText[item.Kind]+"\n", item.Path, item.Detail)
	}
	if len(sum.Items) == 0 {
		b.WriteString("no kept copies and nothing to look at\n")
	}
	code := 0
	if sum.Trouble {
		code = 1
	}
	return b.String(), code
}

// switchReportCommand is `update status` and `update resolve`.
func switchReportCommand(paths Paths, opts options) error {
	if opts.extra == "status" {
		text, code := fenceStatus(fenceControlPath(paths.Data), opts.verbose)
		fmt.Print(text)
		if code != 0 {
			return errors.New("the lines above need you")
		}
		return nil
	}
	found, err := fenceResolve(paths.Data, "", opts.apply)
	for _, r := range found {
		fmt.Printf("switch %s stopped at %q; at data: %s\n", r.Op, r.Phase, r.Live)
		for _, action := range r.Actions {
			fmt.Println("  -", action)
		}
	}
	if err != nil {
		return err
	}
	switch {
	case len(found) == 0:
		fmt.Println("no switch is unfinished")
	case !opts.apply:
		fmt.Println("nothing was changed; run it again with --apply to do the above")
	}
	return nil
}
