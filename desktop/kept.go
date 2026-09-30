package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// keptCopy is one copy of the data folder the switches kept, as the launcher's page lists it: how
// much room it takes, and whether the page may offer to remove it. Every one of them is a whole copy
// of the data, and a failed update's is kept until the operator says otherwise; a card that named
// them without their size gave no reason to look, and no way to act without a terminal.
type keptCopy struct {
	Name  string `json:"name"`
	Kind  string `json:"kind"`
	Bytes int64  `json:"bytes"`
	GC    string `json:"gc"`
}

// keptSizes remembers each record's total by its modification time: a record carries the full
// manifest of a tree, a few megabytes of JSON for a real installation, and the page asks for the
// status every couple of seconds.
var keptSizes = struct {
	sync.Mutex
	by map[string]keptSize
}{by: map[string]keptSize{}}

type keptSize struct {
	at    time.Time
	entry keptCopy
}

// keptCopies lists the recorded copies in the control folder, newest name last. A record that
// cannot be read is left to fenceSummarize, which already reports it.
func keptCopies(control string) []keptCopy {
	metas, _ := filepath.Glob(filepath.Join(control, "retained", "*.json"))
	sort.Strings(metas)
	var out []keptCopy
	keptSizes.Lock()
	defer keptSizes.Unlock()
	for _, path := range metas {
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		if have, ok := keptSizes.by[path]; ok && have.at.Equal(info.ModTime()) {
			out = append(out, have.entry)
			continue
		}
		body, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var meta fenceRetainedMeta
		if json.Unmarshal(body, &meta) != nil || meta.Name == "" {
			continue
		}
		entry := keptCopy{Name: meta.Name, Kind: meta.Kind, GC: meta.GC}
		for _, e := range meta.Manifest {
			if e.Kind == "file" {
				entry.Bytes += e.Size
			}
		}
		keptSizes.by[path] = keptSize{at: info.ModTime(), entry: entry}
		out = append(out, entry)
	}
	return out
}

// RemoveKeptCopy is the page's Remove button: the operator's removal of one kept copy, under a
// fresh fence of its own exactly as the launcher's own removal after an update, and kept — with the
// reason — on anything the fence finds amiss. The operator's word lifts only the "until the
// operator removes it" of a copy kept for them; it never lifts a check.
func (a *App) RemoveKeptCopy(ctx context.Context, name string) error {
	if name == "" || strings.ContainsAny(name, `/\`) || name == "." || name == ".." {
		return errors.New("no such kept copy")
	}
	known := false
	for _, kept := range keptCopies(fenceControlPath(a.paths.Data)) {
		known = known || kept.Name == name
	}
	if !known {
		return fmt.Errorf("%s is not a kept copy of this installation's data", name)
	}
	id, err := a.begin("remove-copy")
	if err != nil {
		return err
	}
	a.log("removing the kept copy %s", name)
	g := fenceCollectRetained(a.paths.Data, "", name, true, nil)
	if g.Outcome != fenceDeleted {
		err = fmt.Errorf("the kept copy %s was not removed (%s): %s", name, g.Outcome, g.Reason)
	} else {
		a.log("the kept copy %s is removed", name)
	}
	a.end(id, err)
	return err
}
