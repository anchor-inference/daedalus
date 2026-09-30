#!/usr/bin/env python3
"""Mutation check for the fenced data switch.

Each mutation removes one guarantee from the source by an exact edit, builds a test binary, and
runs every test that exists to catch that guarantee, each on its own (a panic in one must not hide
the others). A mutation passes the check only if every one of its target tests fails. The sources
are restored whatever happens. Run from desktop/ on ext4:

    TMPDIR=<dir on ext4> python3 fence_mutations.py [name ...]

One JSON line per target, then a summary; exit status 0 only when every target of every mutation
went red and the unmutated control run was green.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# name -> (edits, targets). An edit is (file, old, new, count); count is how many times old must
# occur, so a drifted source makes the check fail loudly instead of mutating nothing.
MUTATIONS = {
    "nolease": ([
        ("fence_linux.go", "unix.FcntlInt(uintptr(fd), unix.F_SETLEASE, unix.F_RDLCK)", "func() (int, error) { return 0, nil }()", 1),
        ("fence_linux.go", "unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0)", "func() (int, error) { return unix.F_RDLCK, nil }()", 3),
        ("fence_control_linux.go", "unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0)", "func() (int, error) { return unix.F_RDLCK, nil }()", 2),
    ], ["TestAHeldSharedMappingIsRefusedAndItsLateWriteStaysLive/fd-closed=false", "TestAHeldSharedMappingIsRefusedAndItsLateWriteStaysLive/fd-closed=true",
        "TestFenceRetainedTreeWithLateMappingIsNeverRemoved"]),
    "nowatch": ([
        ("fence_linux.go", "fmt.Sprintf(\"/proc/self/fd/%d\", node.file.Fd()), fenceMask)", "fmt.Sprintf(\"/proc/self/fd/%d\", node.file.Fd()), unix.IN_MOVE_SELF|unix.IN_ONLYDIR)", 1),
    ], ["TestARootModeChangedAndRestoredAfterTheExchangeRollsBack", "TestFenceNameCreatedWhileListingRefuses/directory",
        "TestFenceNameCreatedWhileListingRefuses/file"]),
    "gcnofence": ([
        ("fence_control_linux.go", "\tleases := 0\n\tr, err := fenceOpenTree(\"R\", retained, name, euid, seams, &leases)",
         "\tif true {\n\t\t_ = os.RemoveAll(filepath.Join(k.path, \"retained\", name))\n\t\t_ = unix.Unlinkat(int(retained.Fd()), name+\".json\", 0)\n\t\tg.Outcome = fenceDeleted\n\t\treturn g\n\t}\n\tleases := 0\n\tr, err := fenceOpenTree(\"R\", retained, name, euid, seams, &leases)", 1),
    ], ["TestFenceRetainedTreeWithLateMappingIsNeverRemoved", "TestFenceWriterAfterExchangeLandsInTheKeptCopy",
        "TestFenceRemovalKeepsTheTreeForANewMappedWriter"]),
    "nomodify": ([
        ("fence_linux.go", "const fenceMask = unix.IN_MODIFY | unix.IN_ATTRIB", "const fenceMask = unix.IN_ATTRIB", 1),
    ], ["TestFenceTruncateThroughTheTreeRefuses"]),
    "nosize": ([
        ("fence_linux.go", "\t\tvar st unix.Stat_t\n\t\tif err := unix.Fstat(int(node.file.Fd()), &st); err != nil || st.Size != node.entry.Size || uint64(st.Nlink) != node.entry.Nlink ||\n\t\t\tst.Mode != node.entry.Mode || st.Uid != node.entry.UID || st.Gid != node.entry.GID || st.Mtim.Nano() != node.entry.MtimeNS {\n\t\t\tresult.add(t.label, node.rel, \"size, link count or metadata changed\")\n\t\t}\n", "", 1),
    ], ["TestFenceLateHardLinkBeforeTheExchangeRefuses", "TestFenceLateHardLinkAfterTheExchangeRollsBack"]),
    "walkfirst": ([
        ("fence_linux.go", "\tif err := t.watch(node); err != nil {\n\t\treturn err\n\t}\n\tif t.seams != nil && t.seams.afterWatch != nil {\n\t\tt.seams.afterWatch(t.label, rel)\n\t}\n\tnames, err := dir.Readdirnames(-1)\n\tif err != nil {\n\t\treturn fenceFail(t.label, rel, \"the directory cannot be listed\", err)\n\t}\n",
         "\tnames, err := dir.Readdirnames(-1)\n\tif err != nil {\n\t\treturn fenceFail(t.label, rel, \"the directory cannot be listed\", err)\n\t}\n\tif t.seams != nil && t.seams.afterWatch != nil {\n\t\tt.seams.afterWatch(t.label, rel)\n\t}\n\tif err := t.watch(node); err != nil {\n\t\treturn err\n\t}\n", 1),
    ], ["TestFenceNameCreatedWhileListingRefuses/directory", "TestFenceNameCreatedWhileListingRefuses/file"]),
    "measurefirst": ([
        ("fence_switch_linux.go", "if diffs := fenceManifestDiff(want, cm); len(diffs) > 0 {", "if diffs := fenceManifestDiff(want, s.record); len(diffs) > 0 && cm != nil {", 1),
    ], ["TestFenceCopyChangedBeforeItsFenceRefuses"]),
    "nofinal": ([
        ("fence_switch_linux.go", "\tsweep = s.sweepBoth(nil, nil)\n\tif fenceSweepFails(sweep) {\n\t\ts.rollback(\"writer activity before the commit\"+fenceOverflowNote(sweep), sweep.Entries, sweep.Overflow)\n\t\treturn\n\t}\n", "", 1),
    ], ["TestFenceWriterBeforeTheFinalSweepRollsBack"]),
    "partialmanifest": ([
        ("fence.go", "a.UID != b.UID || a.GID != b.GID || a.Size != b.Size ||", "a.UID != b.UID || a.Size != b.Size ||", 1),
        ("fence.go", "a.Flags != b.Flags || len(a.Xattrs) != len(b.Xattrs) {", "a.Flags != b.Flags {", 1),
        ("fence.go", "\tfor name, value := range a.Xattrs {\n\t\tif b.Xattrs[name] != value {\n\t\t\treturn false\n\t\t}\n\t}\n", "", 1),
    ], ["TestFenceRemovalKeepsATreeWhoseGroupOrAttributesChanged/attribute", "TestFenceRemovalKeepsATreeWhoseGroupOrAttributesChanged/group"]),
    "rollbackbypath": ([
        ("fence_switch_linux.go", "\tif fenceSameNamed(s.c.root()) != nil || fenceSameNamed(s.p.root()) != nil {\n\t\ts.foreignExchange(reason + \"; and before the rollback the trees were not where the exchange left them\")\n\t\treturn\n\t}\n", "", 1),
        ("fence_switch_linux.go", "if err := unix.Renameat2(int(s.parent.Fd()), s.dataName, int(s.cDir.Fd()), s.cName, unix.RENAME_EXCHANGE); err != nil {\n\t\ts.foreignExchange(reason + \"; and the rollback exchange failed: \" + err.Error())",
         "if err := unix.Renameat2(unix.AT_FDCWD, filepath.Join(s.parentPath, s.dataName), unix.AT_FDCWD, filepath.Join(s.cDir.Name(), s.cName), unix.RENAME_EXCHANGE); err != nil {\n\t\ts.foreignExchange(reason + \"; and the rollback exchange failed: \" + err.Error())", 1),
        ("fence_switch_linux.go", "\tif fenceSameNamed(s.p.root()) != nil || fenceSameNamed(s.c.root()) != nil {\n\t\ts.foreignExchange(reason + \"; and after the rollback the trees were not where it put them\")\n\t\treturn\n\t}\n", "", 1),
    ], ["TestFenceForeignReverseExchangeFailsClosed"]),
    "overflowignored": ([
        ("fence_linux.go", "func fenceObservationVoid(s fenceSweepResult) bool {\n\treturn s.Overflow\n}", "func fenceObservationVoid(s fenceSweepResult) bool {\n\treturn false\n}", 1),
    ], ["TestFenceOverflowBeforeTheExchangeRefuses", "TestFenceInjectedOverflowAfterTheExchangeRollsBack",
        "TestFenceRemovalOverflowBeforeDeletionKeepsTheTree", "TestFenceRemovalOverflowDuringDeletionIsLoud",
        "TestFenceBareOverflowVoidsTheObservation"]),
    # Not one of the named mutations: the same fault one layer down, in the classifier.
    "overflowunclassified": ([
        ("fence_linux.go", "\t\tcase event.Mask&unix.IN_Q_OVERFLOW != 0:\n\t\t\toverflow = true\n", "\t\tcase event.Mask&unix.IN_Q_OVERFLOW != 0:\n", 1),
    ], ["TestFenceBareOverflowVoidsTheObservation"]),
    "rwlock": ([
        ("fence_switch_linux.go", "fd, err := unix.Openat(upgradeFD, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)", "fd, err := unix.Openat(upgradeFD, name, unix.O_RDWR|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)", 1),
    ], ["TestFenceFreeLegacyLockCommits"]),
    "procnotask": ([
        ("fence_proc_linux.go", "tbase := fmt.Sprintf(\"%s/task/%d\", base, tid)", "tbase := base", 1),
    ], ["TestFenceThreadWorkingInThePreviousTreeRollsBack"]),
    "noflags": ([
        ("fence_linux.go", "if flags, err := unix.IoctlGetInt(int(node.file.Fd()), unix.FS_IOC_GETFLAGS); err != nil || uint32(flags)&^fenceServiceFlags != node.entry.Flags {", "if false {", 2),
    ], ["TestFenceFlagChangeAfterTheExchangeRollsBack", "TestFenceFlagChangeBeforeTheExchangeRefuses"]),
    "movenoident": ([
        ("fence_control_linux.go", "\tif err := fenceSameNamed(root); err != nil {\n\t\treturn fmt.Errorf(\"before the move: %w\", err)\n\t}\n", "", 1),
        ("fence_control_linux.go", "\tif err := fenceSameNamed(root); err != nil {\n\t\t_ = unix.Renameat2(int(retained.Fd()), name, int(retained.Fd()), \"foreign-\"+name, unix.RENAME_NOREPLACE)\n\t\treturn fmt.Errorf(\"after the move: %w\", err)\n\t}\n", "", 1),
    ], ["TestFenceForeignExchangeBeforeRetentionFailsClosed"]),
}


def run_target(binary, target, env):
    parts = target.split("/", 1)
    pattern = "^" + parts[0] + "$" + ("/^" + parts[1] + "$" if len(parts) > 1 else "")
    proc = subprocess.run([binary, "-test.run", pattern, "-test.count=1", "-test.v", "-test.timeout=5m"],
                          cwd=HERE, env=env, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    ran = ("--- PASS: " + target) in out or ("--- FAIL: " + target) in out or "panic:" in out
    skipped = ("--- SKIP: " + target) in out
    failed = proc.returncode != 0 and not skipped
    reason = ""
    for line in out.splitlines():
        if "_test.go:" in line and ("Fatal" in line or "want" in line or "outcome" in line or "not" in line):
            reason = line.strip()[:300]
            break
        if line.startswith("panic:"):
            reason = line.strip()[:300]
            break
    return {"ran": ran, "skipped": skipped, "failed": failed, "exit": proc.returncode, "evidence": reason}


def build(tmp, name):
    binary = os.path.join(tmp, name + ".test")
    proc = subprocess.run(["go", "test", "-tags", "nowebview", "-c", "-o", binary, "."], cwd=HERE, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("build failed:\n" + proc.stdout + proc.stderr)
    return binary


def main():
    names = sys.argv[1:] or list(MUTATIONS)
    env = dict(os.environ, DAEDALUS_FENCE_REQUIRE_EXT4="1")
    ok = True
    summary = {}
    with tempfile.TemporaryDirectory(prefix="fence-mutants-") as tmp:
        control = build(tmp, "control")
        for name in names:
            for target in MUTATIONS[name][1]:
                r = run_target(control, target, env)
                if r["failed"] or not r["ran"]:
                    ok = False
                    print(json.dumps({"mutation": "control", "target": target, "verdict": "CONTROL_RED", **r}), flush=True)
        for name in names:
            edits, targets = MUTATIONS[name]
            saved = {}
            try:
                for path, old, new, count in edits:
                    full = os.path.join(HERE, path)
                    if full not in saved:
                        saved[full] = open(full).read()
                    text = open(full).read()
                    found = text.count(old)
                    if found != count:
                        raise RuntimeError(f"{name}: {path}: expected {count} occurrence(s), found {found}")
                    open(full, "w").write(text.replace(old, new))
                binary = build(tmp, name)
            except RuntimeError as e:
                ok = False
                print(json.dumps({"mutation": name, "verdict": "NOT_APPLIED", "error": str(e)[:2000]}), flush=True)
                continue
            finally:
                for full, text in saved.items():
                    open(full, "w").write(text)
            red = 0
            for target in targets:
                r = run_target(binary, target, env)
                verdict = "RED" if r["failed"] else ("SKIPPED" if r["skipped"] else "GREEN")
                red += verdict == "RED"
                print(json.dumps({"mutation": name, "target": target, "verdict": verdict, **r}), flush=True)
            summary[name] = f"{red}/{len(targets)} red"
            ok &= red == len(targets)
    print(json.dumps({"summary": summary, "all_red": ok}), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
