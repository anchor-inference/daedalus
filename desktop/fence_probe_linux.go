//go:build linux

package main

import (
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// fenceProbeResult is what the kernel under us actually does with the three operations the fence
// cannot stop and must see instead.
type fenceProbeResult struct {
	TruncKeepsLease   bool // open(O_RDONLY|O_TRUNC) empties a leased file and the lease survives
	TruncGivesModify  bool // ... and the watched directory gets IN_MODIFY for it
	OutsideLinkSilent bool // a hard link made outside the watched directory gives it no event
	LinkCountSeen     bool // ... but the held descriptor sees st_nlink become 2
}

func (r fenceProbeResult) ok() bool {
	return r.TruncKeepsLease && r.TruncGivesModify && r.OutsideLinkSilent && r.LinkCountSeen
}

// fenceSelfProbe re-establishes, on this very filesystem and kernel, the facts every sweep relies
// on: the size and link-count checks exist because a truncation and a new hard link break no lease,
// and the MODIFY in the mask exists because the truncation is visible there. If the kernel behaves
// otherwise, the reasoning behind the fence does not hold here and the switch fails shut. The probe
// works in a private folder inside the control folder and removes its own two files.
func fenceSelfProbe(k *fenceControl, op string, seams *fenceSeams, leases *int) string {
	var result fenceProbeResult
	dirName := "probe-" + op
	fail := func(what string, err error) string {
		return fmt.Sprintf("the kernel self-check could not run (%s): %v", what, err)
	}
	if err := unix.Mkdirat(int(k.file.Fd()), dirName, 0o700); err != nil {
		return fail("mkdir", err)
	}
	dirFD, err := unix.Openat(int(k.file.Fd()), dirName, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return fail("open", err)
	}
	dir := os.NewFile(uintptr(dirFD), dirName)
	linkName := dirName + "-link"
	defer func() {
		_ = unix.Unlinkat(int(k.file.Fd()), linkName, 0)
		_ = unix.Unlinkat(dirFD, "f", 0)
		dir.Close()
		_ = unix.Unlinkat(int(k.file.Fd()), dirName, unix.AT_REMOVEDIR)
	}()
	w, err := unix.Openat(dirFD, "f", unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
	if err != nil {
		return fail("create", err)
	}
	_, werr := unix.Write(w, []byte("before"))
	unix.Close(w)
	if werr != nil {
		return fail("write", werr)
	}
	tree := &fenceTree{label: "probe", fd: -1, wds: make(map[int]string)}
	tree.source = fenceInotify{tree: tree}
	if tree.fd, err = unix.InotifyInit1(unix.IN_NONBLOCK | unix.IN_CLOEXEC); err != nil {
		return fail("inotify", err)
	}
	defer unix.Close(tree.fd)
	// IN_MODIFY is named on its own: the question here is what the kernel reports, and the answer
	// must not depend on the fence's mask, which the sweep tests separately.
	wd, err := unix.InotifyAddWatch(tree.fd, fmt.Sprintf("/proc/self/fd/%d", dirFD), fenceMask|unix.IN_MODIFY)
	if err != nil {
		return fail("watch", err)
	}
	tree.wds[wd] = "."
	held, err := unix.Openat(dirFD, "f", unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return fail("open", err)
	}
	defer func() {
		_, _ = unix.FcntlInt(uintptr(held), unix.F_SETLEASE, unix.F_UNLCK)
		unix.Close(held)
	}()
	if _, err := unix.FcntlInt(uintptr(held), unix.F_SETLEASE, unix.F_RDLCK); err != nil {
		return fail("lease", err)
	}
	*leases++
	if _, err := tree.readEvents(); err != nil {
		return fail("events", err)
	}
	trunc, err := unix.Openat(dirFD, "f", unix.O_RDONLY|unix.O_TRUNC|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return fail("truncate", err)
	}
	unix.Close(trunc)
	var st unix.Stat_t
	lease, _ := unix.FcntlInt(uintptr(held), unix.F_GETLEASE, 0)
	result.TruncKeepsLease = unix.Fstat(held, &st) == nil && st.Size == 0 && lease == unix.F_RDLCK
	events, _ := tree.readEvents()
	for _, event := range events {
		if event.Name == "f" && event.Mask&unix.IN_MODIFY != 0 {
			result.TruncGivesModify = true
		}
	}
	if err := unix.Linkat(dirFD, "f", int(k.file.Fd()), linkName, 0); err != nil {
		return fail("link", err)
	}
	events, _ = tree.readEvents()
	result.OutsideLinkSilent = len(events) == 0
	result.LinkCountSeen = unix.Fstat(held, &st) == nil && st.Nlink == 2
	if seams != nil && seams.selfProbe != nil {
		seams.selfProbe(&result)
	}
	if !result.ok() {
		return fmt.Sprintf("the kernel does not behave as the fence assumes: %+v", result)
	}
	return ""
}
