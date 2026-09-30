//go:build linux

package main

import (
	"errors"
	"fmt"
	"os"
	"path"
	"strings"

	"golang.org/x/sys/unix"
)

// fenceCopyTree copies the fenced tree P into the empty slot, reading only through P's held
// descriptors. It keeps what the manifest compares — mode, group, extended attributes (ACLs
// included), mtime, symlinks as symlinks — and fails shut, with the reason, where it cannot: a
// generic "the copy differs" would leave the operator nothing to act on.
//
// It returns its own record of what it wrote, measured through its own descriptors. That record is
// what a slot left behind by a refusal is later compared with before it may be removed.
func fenceCopyTree(p *fenceTree, pm fenceManifest, slot *os.File, abort func() error, seams *fenceSeams) (record fenceManifest, err error) {
	record = make(fenceManifest)
	dirs := map[string]*os.File{".": slot}
	defer func() {
		// Whatever happened, the record ends with every directory as we leave it: creating a
		// subdirectory changes its parent's link count after the parent was first measured.
		for rel, dir := range dirs {
			if entry, err := fenceFDEntry(dir, "dir", nil); err == nil {
				record[rel] = entry
			}
			if rel != "." {
				dir.Close()
			}
		}
	}()
	for _, node := range p.dirs[1:] {
		if err := abort(); err != nil {
			return record, err
		}
		parent := dirs[path.Dir(node.rel)]
		name := path.Base(node.rel)
		if err := unix.Mkdirat(int(parent.Fd()), name, 0o700); err != nil {
			return record, fenceFail("C", node.rel, "cannot create", err)
		}
		fd, err := unix.Openat(int(parent.Fd()), name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return record, fenceFail("C", node.rel, "cannot open what was just created", err)
		}
		dirs[node.rel] = os.NewFile(uintptr(fd), node.rel)
		if entry, err := fenceFDEntry(dirs[node.rel], "dir", nil); err == nil {
			record[node.rel] = entry
		}
	}
	if seams != nil && seams.duringCopy != nil {
		seams.duringCopy(p)
	}
	for _, node := range p.files {
		if err := abort(); err != nil {
			return record, err
		}
		if err := fenceCopyFile(node, pm[node.rel], dirs[path.Dir(node.rel)], abort, seams, record); err != nil {
			return record, err
		}
	}
	for _, node := range p.links {
		if err := abort(); err != nil {
			return record, err
		}
		if err := fenceCopyLink(node, pm[node.rel], dirs[path.Dir(node.rel)], record); err != nil {
			return record, err
		}
	}
	// Directories last and deepest first: filling a directory changes its mtime, and a mode without
	// write permission would stop the filling.
	for i := len(p.dirs) - 1; i >= 0; i-- {
		if err := abort(); err != nil {
			return record, err
		}
		node := p.dirs[i]
		dir := dirs[node.rel]
		if err := fenceApplyMeta(node.rel, int(dir.Fd()), int(node.file.Fd()), pm[node.rel], seams); err != nil {
			return record, err
		}
		entry, err := fenceFDEntry(dir, "dir", nil)
		if err != nil {
			return record, fenceFail("C", node.rel, "cannot be measured", err)
		}
		record[node.rel] = entry
	}
	return record, nil
}

func fenceCopyFile(node *fenceNode, want fenceEntry, parent *os.File, abort func() error, seams *fenceSeams, record fenceManifest) error {
	fd, err := unix.Openat(int(parent.Fd()), node.name, unix.O_RDWR|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
	if err != nil {
		return fenceFail("C", node.rel, "cannot create", err)
	}
	out := os.NewFile(uintptr(fd), node.rel)
	defer out.Close()
	src := int(node.file.Fd())
	if seams != nil && seams.copyWrite != nil {
		if err := seams.copyWrite(node.rel); err != nil {
			fenceRecordPartial(out, node.rel, record)
			return fenceFail("C", node.rel, "cannot write the copy", err)
		}
	}
	buf := make([]byte, 1<<20)
	for off := int64(0); off < want.Size; {
		if err := abort(); err != nil {
			fenceRecordPartial(out, node.rel, record)
			return err
		}
		n, err := unix.Pread(src, buf, off)
		if n > 0 {
			if _, werr := unix.Pwrite(fd, buf[:n], off); werr != nil {
				fenceRecordPartial(out, node.rel, record)
				return fenceFail("C", node.rel, "cannot write the copy", werr)
			}
			off += int64(n)
		}
		if err != nil {
			fenceRecordPartial(out, node.rel, record)
			return &fenceProblem{Outcome: fenceRefused, Tree: "P", Path: node.rel, Cause: "cannot be read", Err: err}
		}
		if n == 0 {
			fenceRecordPartial(out, node.rel, record)
			return &fenceProblem{Outcome: fenceRefused, Tree: "P", Path: node.rel, Cause: "shorter than recorded: changed during the copy"}
		}
	}
	if err := fenceApplyMeta(node.rel, fd, src, want, seams); err != nil {
		fenceRecordPartial(out, node.rel, record)
		return err
	}
	entry, err := fenceFDEntry(out, "file", abort)
	if err != nil {
		return fenceFail("C", node.rel, "cannot be measured", err)
	}
	record[node.rel] = entry
	return nil
}

// fenceRecordPartial notes a file the copy stopped in the middle of, as it stands, so that the slot
// can still be compared with what we know we left there.
func fenceRecordPartial(out *os.File, rel string, record fenceManifest) {
	if entry, err := fenceFDEntry(out, "file", nil); err == nil {
		record[rel] = entry
	}
}

// fenceApplyMeta gives a new inode the metadata of its original, in the one order that works: the
// group before the mode, because changing the group clears set-group-id; the attributes before the
// mode, because an access ACL rewrites the group bits; the mtime last.
func fenceApplyMeta(rel string, fd, src int, want fenceEntry, seams *fenceSeams) error {
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return fenceFail("C", rel, "stat", err)
	}
	if st.Gid != want.GID {
		var err error
		if seams != nil && seams.fchownGroup != nil {
			err = seams.fchownGroup(rel)
		}
		if err == nil {
			err = unix.Fchown(fd, -1, int(want.GID))
		}
		if err != nil {
			return fenceFail("C", rel, fmt.Sprintf("the group %d of this entry is not available to this user", want.GID), err)
		}
	}
	if err := fenceCopyXattrs(rel, src, fd); err != nil {
		return err
	}
	if err := unix.Fchmod(fd, want.Mode&0o7777); err != nil {
		return fenceFail("C", rel, "the mode cannot be set", err)
	}
	if want.Kind == "file" || want.Kind == "dir" {
		mtime := want.MtimeNS
		if want.Kind == "dir" {
			var srcSt unix.Stat_t
			if err := unix.Fstat(src, &srcSt); err == nil {
				mtime = srcSt.Mtim.Nano()
			}
		}
		times := []unix.Timespec{{Nsec: unix.UTIME_OMIT}, unix.NsecToTimespec(mtime)}
		if err := unix.UtimesNanoAt(unix.AT_FDCWD, fmt.Sprintf("/proc/self/fd/%d", fd), times, 0); err != nil {
			return fenceFail("C", rel, "the modification time cannot be set", err)
		}
	}
	return nil
}

func fenceRawXattrs(list func([]byte) (int, error), get func(string, []byte) (int, error)) (map[string][]byte, error) {
	size, err := list(nil)
	if err != nil || size == 0 {
		return nil, err
	}
	buf := make([]byte, size)
	n, err := list(buf)
	if err != nil {
		return nil, err
	}
	attrs := make(map[string][]byte)
	for _, name := range strings.Split(string(buf[:n]), "\x00") {
		if name == "" {
			continue
		}
		vlen, err := get(name, nil)
		if err != nil {
			return nil, err
		}
		value := make([]byte, vlen)
		if vlen > 0 {
			if _, err := get(name, value); err != nil {
				return nil, err
			}
		}
		attrs[name] = value
	}
	return attrs, nil
}

// fenceCopyXattrs makes the copy's attributes exactly the original's: every one set, and every one
// the new inode picked up on its own (an inherited default ACL, a label) removed unless the original
// has it too.
func fenceCopyXattrs(rel string, src, dst int) error {
	want, err := fenceRawXattrs(func(b []byte) (int, error) { return unix.Flistxattr(src, b) },
		func(n string, b []byte) (int, error) { return unix.Fgetxattr(src, n, b) })
	if err != nil {
		return fenceFail("P", rel, "extended attributes cannot be read", err)
	}
	for name, value := range want {
		if err := unix.Fsetxattr(dst, name, value, 0); err != nil {
			if name == "security.selinux" {
				return fenceFail("C", rel, "the SELinux label differs and cannot be set on the copy", err)
			}
			return fenceFail("C", rel, "the extended attribute "+name+" cannot be copied", err)
		}
	}
	have, err := fenceRawXattrs(func(b []byte) (int, error) { return unix.Flistxattr(dst, b) },
		func(n string, b []byte) (int, error) { return unix.Fgetxattr(dst, n, b) })
	if err != nil {
		return fenceFail("C", rel, "extended attributes cannot be read", err)
	}
	for name := range have {
		if _, ok := want[name]; ok {
			continue
		}
		if err := unix.Fremovexattr(dst, name); err != nil {
			if name == "security.selinux" {
				return fenceFail("C", rel, "the SELinux label differs and cannot be set on the copy", err)
			}
			return fenceFail("C", rel, "the extended attribute "+name+" appeared on the copy and cannot be removed", err)
		}
	}
	return nil
}

func fenceCopyLink(node *fenceNode, want fenceEntry, parent *os.File, record fenceManifest) error {
	if err := unix.Symlinkat(want.Target, int(parent.Fd()), node.name); err != nil {
		return fenceFail("C", node.rel, "cannot create the symlink", err)
	}
	var st unix.Stat_t
	if err := unix.Fstatat(int(parent.Fd()), node.name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return fenceFail("C", node.rel, "stat", err)
	}
	if st.Gid != want.GID {
		if err := unix.Fchownat(int(parent.Fd()), node.name, -1, int(want.GID), unix.AT_SYMLINK_NOFOLLOW); err != nil {
			return fenceFail("C", node.rel, fmt.Sprintf("the group %d of this entry is not available to this user", want.GID), err)
		}
	}
	if len(want.Xattrs) > 0 {
		// Only trusted.* and security.* can sit on a symlink, and copying them needs privileges this
		// launcher does not have.
		return fenceFail("C", node.rel, "a symlink with extended attributes cannot be copied", nil)
	}
	times := []unix.Timespec{{Nsec: unix.UTIME_OMIT}, unix.NsecToTimespec(want.MtimeNS)}
	if err := unix.UtimesNanoAt(int(parent.Fd()), node.name, times, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return fenceFail("C", node.rel, "the modification time cannot be set", err)
	}
	copied := &fenceNode{rel: node.rel, kind: "symlink", parent: parent, name: node.name, dev: uint64(st.Dev), ino: st.Ino}
	entry, err := (&fenceTree{}).linkEntry(copied)
	if err != nil {
		if errors.Is(err, unix.ENOENT) {
			return &fenceProblem{Outcome: fenceRefused, Tree: "C", Path: node.rel, Cause: "removed while it was being copied", Err: err}
		}
		return fenceFail("C", node.rel, "cannot be measured", err)
	}
	record[node.rel] = entry
	return nil
}
