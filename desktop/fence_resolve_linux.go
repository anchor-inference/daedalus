//go:build linux

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

// fenceResolution is what `update resolve` found for one switch that did not finish, and what it
// does (or, without --apply, would do) about it.
type fenceResolution struct {
	Op      string    `json:"op"`
	Phase   string    `json:"phase"`
	Live    string    `json:"live"` // what is at data: P, C, a foreign tree, or nothing
	Actions []string  `json:"actions"`
	Applied bool      `json:"applied"`
	At      time.Time `json:"at"`
}

// fenceResolve settles every switch whose journal did not reach its end — a crash, a power cut, a
// third party who exchanged the trees. Its defaults are the safe ones, and there is no option to
// change them:
//
//   - whatever is at data stays there and stays live. After a crash nothing was fenced any more, and
//     the agent may already have run on the tree at data; putting the other one back could discard
//     what it wrote. The other tree is kept, and the operator can still restore it by hand;
//   - each of our trees found where the journal says, by inode, is filed under retained for the
//     operator (gc: manual). A tree whose inode is not ours is left where it is and named;
//   - nothing is deleted.
//
// Without apply it only reports. With apply it acts and marks the journal resolved, which is what
// lets the next switch start.
func fenceResolve(data, control string, apply bool) ([]fenceResolution, error) {
	data = filepath.Clean(data)
	parentPath, dataName := filepath.Dir(data), filepath.Base(data)
	pfd, err := unix.Open(parentPath, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	parent := os.NewFile(uintptr(pfd), parentPath)
	defer parent.Close()
	var st unix.Stat_t
	if err := unix.Fstat(pfd, &st); err != nil {
		return nil, err
	}
	if control == "" {
		control = ".daedalus-update"
	}
	if _, err := os.Lstat(filepath.Join(parentPath, control)); err != nil {
		return nil, nil // no switch has ever run here
	}
	k, reason := fenceOpenControl(parent, control, uint32(os.Geteuid()), uint64(st.Dev), nil)
	if reason != "" {
		return nil, fmt.Errorf("%s", reason)
	}
	defer k.close()
	if busy := k.lock(); busy != "" {
		return nil, fmt.Errorf("%s", busy)
	}
	defer k.unlock()
	names, err := k.sub["journal"].Readdirnames(-1)
	if err != nil {
		return nil, err
	}
	sort.Strings(names)
	var out []fenceResolution
	for _, name := range names {
		if !strings.HasSuffix(name, ".json") {
			continue
		}
		body, err := k.readFile("journal", name)
		if err != nil {
			return out, err
		}
		var j fenceJournal
		if err := json.Unmarshal(body, &j); err != nil {
			return out, fmt.Errorf("the journal %s is damaged: %w", name, err)
		}
		if fenceJournalFinished(j.Phase) {
			continue
		}
		r := fenceResolution{Op: j.Op, Phase: j.Phase, At: time.Now().UTC()}
		slotDir := parent
		if j.SlotRetained {
			slotDir = k.sub["retained"]
		}
		identifyIn := func(dir *os.File, name string) string {
			if name == "" {
				return "nothing"
			}
			var st unix.Stat_t
			if unix.Fstatat(int(dir.Fd()), name, &st, unix.AT_SYMLINK_NOFOLLOW) != nil {
				return "nothing"
			}
			switch [2]uint64{uint64(st.Dev), st.Ino} {
			case j.P:
				return "P"
			case j.C:
				return "C"
			}
			return "a foreign tree"
		}
		r.Live = identifyIn(parent, dataName)
		atSlot := identifyIn(slotDir, j.Slot)
		r.Actions = append(r.Actions, fmt.Sprintf("leave %s at data, live, untouched", r.Live))
		switch {
		case j.SlotRetained && atSlot == "C":
			// A restore that stopped before its exchange: the kept copy is still kept, as it was.
			r.Actions = append(r.Actions, fmt.Sprintf("leave the kept copy %s where it is", filepath.Join(k.path, "retained", j.Slot)))
		case j.SlotRetained && atSlot == "P":
			// A restore that stopped after its exchange: the copy from before the update is live, and
			// what the failed version left sits under the kept copy's name.
			target := filepath.Join(k.path, "retained", "failed-"+j.Op)
			r.Actions = append(r.Actions, fmt.Sprintf("file the data the failed version left as %s, for the operator", target))
			if apply {
				holder := &fenceTree{label: "P", dirs: []*fenceNode{{rel: ".", kind: "dir", parent: slotDir, name: j.Slot, dev: j.P[0], ino: j.P[1]}}}
				if err := k.adopt(holder, "failed-"+j.Op, j.Op, "failed", nil, "manual", fmt.Sprintf("left by a restore that stopped at %q; kept by `update resolve`", j.Phase)); err != nil {
					return out, fmt.Errorf("filing %s: %w", j.Slot, err)
				}
				k.forget(j.Slot)
			}
		case atSlot == "P" || atSlot == "C":
			kind := map[string]string{"P": "pre", "C": "slot"}[atSlot]
			if atSlot == "C" && j.Phase != "started" && j.Phase != "fencing" {
				kind = "rejected"
			}
			target := filepath.Join(k.path, "retained", kind+"-"+j.Op)
			r.Actions = append(r.Actions, fmt.Sprintf("file %s (%s, at %s) as %s, for the operator to keep or delete", atSlot, map[string]string{"P": "the tree from before the switch", "C": "the switch's copy"}[atSlot], filepath.Join(parentPath, j.Slot), target))
			if apply {
				key := j.P
				if atSlot == "C" {
					key = j.C
				}
				holder := &fenceTree{label: atSlot, dirs: []*fenceNode{{rel: ".", kind: "dir", parent: slotDir, name: j.Slot, dev: key[0], ino: key[1]}}}
				reason := fmt.Sprintf("left by a switch that stopped at %q; kept by `update resolve`", j.Phase)
				if err := k.adopt(holder, kind+"-"+j.Op, j.Op, kind, nil, "manual", reason); err != nil {
					return out, fmt.Errorf("filing %s: %w", j.Slot, err)
				}
			}
		case atSlot == "a foreign tree":
			r.Actions = append(r.Actions, fmt.Sprintf("leave %s alone: it is not one of this switch's trees", filepath.Join(parentPath, j.Slot)))
		}
		for _, other := range []string{"P", "C"} {
			if r.Live != other && atSlot != other {
				r.Actions = append(r.Actions, fmt.Sprintf("%s is neither at data nor at the slot; if the switch filed it, it is under %s", other, filepath.Join(k.path, "retained")))
			}
		}
		r.Actions = append(r.Actions, "mark the switch resolved, so that the next one may start")
		if apply {
			j.Phase = "resolved"
			k.journal(j)
			r.Applied = true
			body, _ := json.MarshalIndent(r, "", "  ")
			if err := k.writeFile("reports", j.Op+"-resolved.json", body); err != nil {
				return out, err
			}
		}
		out = append(out, r)
	}
	return out, nil
}
