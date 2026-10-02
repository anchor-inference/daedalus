#!/usr/bin/env bash
# Scan the working tree and the full git history for things that must not be public:
# secrets, internal addresses, tooling trailers, machine paths, and build artefacts.
# Run before opening the repository, and before any push.
#
# Every check gates. An exit of 0 is the claim that this tree may be published, so a check that only
# prints is worse than no check: it is the one that was passing when `desktop/desktop` went in.
#
# Four checks, in the order they are printed:
#
#   patterns   text that is a secret or an internal address, in the tree, in every blob in history,
#              and in every commit message. The project's own `Co-authored-by: Daedalus` trailer is
#              what the agent signs its commits with and is meant to be there; every other
#              co-author line, and every session link, is not.
#   binaries   a tracked file git treats as binary that is not one of the assets this repository is
#              supposed to carry, plus anything at all whose first bytes say ELF, Mach-O or PE. A
#              compiled binary is not just weight: an unstripped one carries the absolute path of
#              every source file it was built from, which is the machine it was built on.
#   paths      /home/<name> and /Users/<name> outside the placeholder names the docs and tests use
#              deliberately, and — separately, and without ever writing it down here — the name of
#              the account this is being run from.
#
# `--self-check` runs the whole thing against a fixture repository carrying one of each fault, and
# fails unless every rule fires. That is the part that keeps this honest: a scanner nobody has seen
# fail is a scanner nobody knows works.
set -euo pipefail

PATTERNS='sk-[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]{20,}|[0-9]{6,}:[A-Za-z0-9_-]{30,}|192\.168\.[0-9.]+|10\.10\.[0-9.]+|Co-authored-by|Generated with|[Cc]laude-[Ss]ession|session_01|claude\.ai/code|Signed-off-by'

# Where this repository legitimately keeps bytes git cannot diff. Anchored at the start of the path,
# so a binary that merely ends in one of these names does not slip through on the suffix. The
# screenshots may sit one level down, in a folder named by a language code: the same set in Russian.
# Bundled terminal and installer fonts carry their licences beside them. The installer's still
# image is its fallback without WebGL; executable images remain forbidden even at an asset path.
BINARY_ALLOWED='^docs/(brand|diagrams|screenshots(/[a-z]{2})?)/[^/]+\.(png|jpg|jpeg|webp|gif|svg)$|^miniapp/public/.+\.(png|jpg|ico|webp|svg)$|^miniapp/src/terminal/fonts/[^/]+\.woff2$|^desktop/ui/assets/setup/(still\.webp|fonts/geist(-mono)?-(cyrillic|latin)\.woff2)$|^skills/.+\.(png|jpg|jpeg|webp|gif|ttf|otf|woff2?|pdf|tar\.gz|zip)$'

# Usernames that appear in documentation and tests on purpose, as examples. Everything else is a real
# account name and has no business being committed.
PLACEHOLDER_USERS='someone|operator|keyproxy|user|admin|ada|you|a|o|x|y|z'

# Files whose whole point is to carry one of the patterns above: the scanner itself, the agent's own
# commit-trailer documentation, and the fixtures the redaction tests are built out of.
PATTERN_EXEMPT='(scripts/audit_public\.sh|README\.md|daedalus/extensions/selfdev\.py|skills/self-develop/SKILL\.md|tests/)'

OWN_TRAILER='Co-authored-by: daedalus'

# Anything whose first bytes are one of these is an executable image, wherever it sits and whatever it
# is called. `desktop/desktop` had no extension to catch it by.
is_executable_image() {
  local head
  head=$(head -c 4 -- "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')
  case "$head" in
    7f454c46*) return 0 ;;   # ELF
    cffaedfe*|cefaedfe*|cafebabe*) return 0 ;;   # Mach-O, and a fat binary
    4d5a*) return 0 ;;       # PE / MZ
    *) return 1 ;;
  esac
}

audit() {
  local failed=0

  echo "== working tree"
  if git grep -InE "$PATTERNS" -- . 2>/dev/null | grep -EvI "$PATTERN_EXEMPT" | grep -vi "$OWN_TRAILER"; then failed=1; else echo "clean"; fi

  echo "== history (all blobs)"
  local history_hits history_report walked unreadable commit_list enum_rc shallow duplicate_ids
  # Captured, then tested -- never tested by the pipeline's status. Under pipefail the status of
  # `cmd | while ...; done | grep ...` is the status of the loop's LAST iteration, so a pattern
  # present in an older commit and absent from the newest one printed its line here and answered
  # "clean": the verdict hung on the order `git rev-list --all` happens to visit commits in.
  # The walk that reads is also the walk that counts. A finding proves only that the reader reached
  # the commit the finding is in, so a walk cut short after it prints the same line and answers the
  # same way; the number of commits visited is the only part of the answer that does not depend on
  # where the finding sits. It is taken inside the substitution that does the grepping, because a
  # count read from a second, untruncated enumeration would agree with itself and prove nothing.
  # The enumeration's own status is taken where the list is produced. `< <(git rev-list --all)`
  # hides it and `$(... || true)` below hides it again, so an enumeration that failed printed the
  # same two words as one that found nothing: "clean" over a section that never ran. A list that
  # came back non-zero, and a commit the reader could not read, are both a reader that cannot
  # answer -- a commit nobody could read is not a commit that was cleared.
  commit_list=$(mktemp)
  enum_rc=0
  git rev-list --all > "$commit_list" || enum_rc=$?
  # A list of the right length is not a list of the right commits. An enumeration that named one
  # commit as many times as the repository has commits would walk the expected number of lines,
  # read every one without error, and never visit the finding. So the walk reads the distinct ids
  # -- the number printed below counts commits, not lines -- and repeats are refused outright
  # rather than quietly deduplicated, because a substituted list is not a repository to be tidied.
  # `git rev-list` never repeats an id, so this cannot fire on a healthy tree.
  duplicate_ids=$(sort "$commit_list" | uniq -d)
  history_report=$(
    walked=0
    unreadable=0
    while read -r c; do
      walked=$((walked + 1))
      out=""
      rc=0
      out=$(git grep -InE "$PATTERNS" "$c" -- . 2>/dev/null) || rc=$?
      [ "$rc" -le 1 ] || unreadable=$((unreadable + 1))
      # `git grep <commit>` already prefixes every line it prints with the commit it found it in,
      # so the line goes out as it comes back. Prefixing it again printed the same id twice:
      # `<sha>:<sha>:<path>:<line>:<text>`, which a reader comparing this output with plain
      # `git grep` sees as a prefix the tool did not generate.
      [ -z "$out" ] || printf '%s\n' "$out"
    done < <(sort -u "$commit_list")
    printf 'walked %d\nunreadable %d\n' "$walked" "$unreadable"
  )
  rm -f "$commit_list"
  walked=$(printf '%s\n' "$history_report" | sed -n 's/^walked \([0-9][0-9]*\)$/\1/p')
  unreadable=$(printf '%s\n' "$history_report" | sed -n 's/^unreadable \([0-9][0-9]*\)$/\1/p')
  history_hits=$(printf '%s\n' "$history_report" | grep -v '^walked [0-9][0-9]*$' |
    grep -v '^unreadable [0-9][0-9]*$' | grep -EvI "$PATTERN_EXEMPT" | grep -vi "$OWN_TRAILER" || true)
  echo "history: ${walked:-0} commit(s) walked"
  # A walk cannot tell a cut from a small repository: `--max-count=1` and a one-commit
  # repository print the same line and answer the same way, and nothing inside the walk
  # separates them. The commit-object store does not either, and it was tried and rejected:
  # 353 commits walked against 367 stored in one repository, 1054 against 1563 in another,
  # because a store keeps commits no ref reaches after a rebase or a gc -- a reading that
  # fires on healthy trees is a reading that gets switched off.
  #
  # What can be separated is a checkout whose history git itself records as short. A depth-1
  # clone walks every commit it has, reports one, and would answer "clean" over a history it
  # never saw; the credential in the oldest commit is simply not there to be found. So the
  # shallowness is read, and the verdict is never printed without the number it rests on.
  shallow=$(git rev-parse --is-shallow-repository 2>/dev/null || echo unknown)
  if [ -n "$duplicate_ids" ]; then
    echo "history: the enumeration named the same commit more than once (${duplicate_ids} ) -- a list of the right length is not a list of the right commits, so this section certifies nothing"
    failed=1
  elif [ "$shallow" = true ]; then
    echo "history: this checkout is shallow -- older commits are not here to be read, so this section certifies nothing"
    failed=1
  elif [ "$enum_rc" -ne 0 ] || [ "${unreadable:-0}" -ne 0 ]; then
    echo "history: the reader could not answer -- enumeration exit $enum_rc, ${unreadable:-0} commit(s) unreadable"
    failed=1
  elif [ -n "$history_hits" ]; then printf '%s\n' "$history_hits"; failed=1
  else echo "history: clean over the ${walked:-0} commit(s) this checkout has"; fi

  echo "== commit messages"
  if git log --all --format='%H %s%n%b' | grep -inE "$PATTERNS" | grep -vi "$OWN_TRAILER"; then failed=1; else echo "clean"; fi

  echo "== tracked binaries"
  local found=""
  # The empty tree: diffing HEAD against it lists every tracked file, and numstat prints "-\t-" for
  # each one git cannot produce a line count for — which is exactly its own definition of binary.
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if ! printf '%s' "$path" | grep -qE "$BINARY_ALLOWED"; then
      found="$found$path (binary, not an allowed asset)"$'\n'
    fi
  done < <(git diff --numstat 4b825dc642cb6eb9a060e54bf8d69288fbee4904 HEAD 2>/dev/null | awk -F'\t' '$1=="-" && $2=="-" {print $3}')
  while IFS= read -r path; do
    [ -f "$path" ] || continue
    if is_executable_image "$path"; then
      found="$found$path (compiled executable — it carries the paths it was built from)"$'\n'
    fi
  done < <(git ls-files)
  if [ -n "$found" ]; then printf '%s' "$found"; failed=1; else echo "clean"; fi

  echo "== machine paths"
  local hits
  # The name must begin with a letter: a URL path like news/home/20220228005327 is not an account.
  hits=$(git grep -InE '/(home|Users)/[a-z][A-Za-z0-9_.-]*' -- . 2>/dev/null |
    grep -vE 'scripts/audit_public\.sh' |
    grep -EvI "/(home|Users)/($PLACEHOLDER_USERS)([^A-Za-z0-9_.-]|$)" || true)
  # The one name that must never appear is read from the environment rather than written down: a
  # scanner that spells out the account it is guarding against publishes it in the file that guards it.
  local me home
  me=$(id -un 2>/dev/null || echo "")
  home=$(basename -- "${HOME:-/}")
  for name in "$me" "$home"; do
    [ -n "$name" ] && [ "$name" != "/" ] || continue
    hits="$hits$(git grep -InF "/home/$name" -- . 2>/dev/null | grep -vE 'scripts/audit_public.sh' || true)"
    hits="$hits$(git grep -InF "/Users/$name" -- . 2>/dev/null | grep -vE 'scripts/audit_public.sh' || true)"
  done
  hits=$(printf '%s\n' "$hits" | grep -v '^$' || true)
  if [ -n "$hits" ]; then printf '%s\n' "$hits"; failed=1; else echo "clean"; fi

  return "$failed"
}

# -- the self-check ---------------------------------------------------------------------------
#
# Builds a throwaway repository holding one of each fault the audit above looks for -- a committed
# ELF binary, a file naming the account this is running as, a provider token, an internal address,
# and a commit message carrying tooling trailers -- and requires the audit to refuse every one of
# them by name. Then requires the real tree to pass.
#
# One fault per rule, and the rules are the whole of `PATTERNS`: a fixture carrying only the faults
# the scanner was written against first proves nothing about the rest of the list. Measured before
# this fixture was widened: deleting all four provider-token patterns from `PATTERNS` left this
# self-check green, so a repository carrying a token was scanned clean by an audit that still said
# it had checked itself.

self_check() {
  local fixture status kinds file
  fixture=$(mktemp -d)
  (
    cd "$fixture"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf '\177ELF\002\001\001\000 built somewhere\n' > tool
    mkdir -p notes
    printf 'the build ran in /home/%s/checkout\n' "$(id -un)" > notes/build.md
    # One credential-shaped line per kind `PATTERNS` knows, and one file naming an address on each
    # of the two private blocks it looks for. Every value here is ASSEMBLED at run time rather than
    # written down, for the same reason the account name above is read from the environment: the
    # file that guards against a value has no business carrying it. None of these is real; each is
    # a repeated byte behind the prefix that gives it its shape.
    #
    # One kind per FILE, and not one file carrying all four. With them in one file the arm below
    # asked only whether the report named that file, and the report went on naming it as long as
    # any single alternative still matched a line inside it: cutting `github_pat_` out of the rule
    # left the other three lines matching, `config.env` was named, and the self-check was green
    # over a rule that had stopped matching a whole kind. Measured -- the surviving substitution is
    # what put the kinds in separate files. Now each file carries one kind, so the report names a
    # file only while the alternative that matches it is in the rule, and the arm fails by name.
    filler=$(printf 'A%.0s' $(seq 1 40))
    printf '%s_%s\n' ghp "$filler"        > config-ghp.env
    printf '%s-%s\n' sk "$filler"         > config-sk.env
    printf '%s_%s\n' github_pat "$filler" > config-pat.env
    printf '%s:%s\n' 1234567 "$filler"    > config-npm.env
    printf 'the console answers at http://%s.%s.9.9:8080 and the proxy at http://%s.%s.0.9:9000\n' \
      192 168 10 10 > notes/network.md
    git add -A
    git commit -qm "a fixture

Co-authored-by: someone <someone@example.invalid>
Generated with a tool: see the session log at https://example.invalid/session_01"
  )
  # The expected carriers are an independent contract. A catalog made only from files that
  # happened to be written cannot notice a kind whose writer was removed.
  local expected_kinds missing_carriers
  expected_kinds=$(printf '%s\n' config-ghp.env config-npm.env config-pat.env config-sk.env | sort)
  kinds=$(cd "$fixture" && for file in config-*.env; do
    [ -f "$file" ] && printf '%s\n' "$file"
  done | sort)
  missing_carriers=$(comm -23 <(printf '%s\n' "$expected_kinds") <(printf '%s\n' "$kinds"))
  if [ -n "$missing_carriers" ]; then
    echo "SELF-CHECK FAILED: fixture carrier missing: $(printf '%s' "$missing_carriers" | tr '\n' ' ')"
    rm -rf "$fixture"
    return 1
  fi
  status=0
  bash "$SELF" "$fixture" > "$fixture/out.txt" 2>&1 || status=$?
  local report
  report=$(cat "$fixture/out.txt")
  rm -rf "$fixture"
  if [ "$status" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a repository with a committed binary and a machine path"
    printf '%s\n' "$report"
    return 1
  fi
  printf '%s' "$report" | grep -q "tool (compiled executable" || {
    echo "SELF-CHECK FAILED: the committed binary was not named"; printf '%s\n' "$report"; return 1; }
  printf '%s' "$report" | grep -q "notes/build.md" || {
    echo "SELF-CHECK FAILED: the machine path was not named"; printf '%s\n' "$report"; return 1; }
  # One assertion per kind, each naming the file that carries it. A single assertion over all four
  # kinds in one file answered "the provider token was named" while a kind had stopped being
  # matched, so the name that must appear in the failure is the one that went missing.
  local missing="" named=""
  while read -r file; do
    [ -n "$file" ] || continue
    if printf '%s' "$report" | grep -q "$file"; then
      named="$named $file"
    else
      missing="$missing $file"
    fi
  done <<< "$kinds"
  if [ -n "$missing" ]; then
    echo "SELF-CHECK FAILED: the audit did not name the kinds these files carry:$missing -- the alternative that matches each is not in the rule, and nothing in the run says so"
    printf '%s\n' "$report"
    return 1
  fi
  echo "self-check: the kinds the report names, one per file:$named"
  printf '%s' "$report" | grep -q "notes/network.md" || {
    echo "SELF-CHECK FAILED: the internal address was not named"; printf '%s\n' "$report"; return 1; }
  printf '%s' "$report" | grep -q "someone@example.invalid" || {
    echo "SELF-CHECK FAILED: the tooling trailer was not named"; printf '%s\n' "$report"; return 1; }
  echo "self-check: the audit refuses a committed binary and a machine path, each provider token kind by the file that carries it, an internal address and a tooling trailer"

  local out
  out=$(bash "$SELF" "$ROOT" 2>&1) || {
    echo "SELF-CHECK FAILED: the audit does not pass this tree"
    printf '%s\n' "$out"
    return 1
  }
  echo "self-check: the audit passes this tree"

  # A place, not a family: the same rule, applied where the fixture never puts it. Every fault in the
  # fixture above is committed, so it lives in the working tree AND in history at once and nothing in
  # it separates "the history reader works" from "it is dead and another check caught the bytes".
  # Here the pattern exists ONLY in an older commit: it is in no file of the tree and in no message.
  local older status2 report2
  older=$(mktemp -d)
  (
    cd "$older"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    git commit -q --allow-empty -m "an empty tree"
    printf 'a key %s_%s\n' ghp "$(printf 'A%.0s' $(seq 1 40))" > gone.txt
    git add -A
    git commit -qm "a file that will not stay"
    git rm -q gone.txt
    git commit -qm "and is gone from the tree"
  )
  local walked_expected
  walked_expected=$(cd "$older" && git rev-list --all | wc -l | tr -d ' ')
  status2=0
  bash "$SELF" "$older" > "$older/out.txt" 2>&1 || status2=$?
  report2=$(cat "$older/out.txt")
  rm -rf "$older"
  if [ "$status2" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a repository whose only credential is in an older commit"
    printf '%s\n' "$report2"
    return 1
  fi
  # The credential is in the fixture's middle commit, so the refusal above witnesses only the region
  # that commit sits in: an enumeration cut short after it still finds the credential and still
  # refuses. This is the other half, and it holds wherever the credential sits -- the audit must
  # report having visited every commit the fixture has.
  if ! printf '%s\n' "$report2" | grep -q "^history: $walked_expected commit(s) walked$"; then
    echo "SELF-CHECK FAILED: the history reader did not report visiting all of the fixture's commits"
    printf '%s\n' "$report2"
    return 1
  fi
  echo "self-check: the audit refuses a pattern that survives only in history"

  # The same separation, asked of the other place. Every credential in the first fixture is
  # committed, so it lives in the working tree and in history at once: with the working-tree reader
  # replaced by a no-op (`git grep -InE "$PATTERNS" -- .` -> `printf ""`) the self-check still
  # passed, because the history reader printed the same file names and every assertion above rests
  # on a name, not on the section that printed it. Measured, and the reason this arm exists. Here
  # the credential is in the working tree of a TRACKED file and in no commit at all, so the only
  # reader that can name it is the one under test -- and the assertion is that the section which
  # reads the working tree is where it is named. (A file that is untracked would do the separation
  # too, and would be found by neither section: `git grep` reads tracked files.)
  local live status3 report3
  live=$(mktemp -d)
  # The build itself is in the condition, and its result is checked below: under `set -e` a subshell
  # whose `git commit` refused would end the self-check silently, since the caller never gets to run.
  # The other arms that build fixtures have the same shape; this one at least names its own failure.
  if ! (
    cd "$live"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf 'a file with nothing in it\n' > notes.txt
    git add -A
    git commit -qm "a clean tree"
    printf 'a key %s_%s\n' ghp "$(printf 'A%.0s' $(seq 1 40))" > notes.txt
  ); then
    echo "SELF-CHECK FAILED: the working-tree-only fixture could not be built"
    rm -rf "$live"
    return 1
  fi
  # The build above cannot prove the fixture landed: with `false` in place of the commit the subshell
  # still exits 0, because the whole `if ! ( ... )` sits in a condition context where `set -e` does not
  # fire (measured — the first version of this guard trusted it and the arm passed a fixture with no
  # commit at all). So the state is read rather than assumed: exactly one tracked file modified and
  # unstaged, the credential in the working tree, and no credential in the commit. An arm satisfied by
  # a fixture that never landed is the defect this script exists to refuse.
  local live_status live_committed
  live_status=$(git -C "$live" status --porcelain 2>/dev/null || printf 'no working tree')
  live_committed=$(git -C "$live" show HEAD:notes.txt 2>/dev/null || printf 'no commit')
  if [ "$live_status" != " M notes.txt" ] \
     || ! grep -qE "$PATTERNS" "$live/notes.txt" \
     || printf '%s' "$live_committed" | grep -qE "$PATTERNS"; then
    echo "SELF-CHECK FAILED: the working-tree-only fixture did not land as the arm describes it"
    rm -rf "$live"
    return 1
  fi
  status3=0
  bash "$SELF" "$live" > "$live/out.txt" 2>&1 || status3=$?
  report3=$(cat "$live/out.txt")
  rm -rf "$live"
  if [ "$status3" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a repository whose only credential is uncommitted in its working tree"
    printf '%s\n' "$report3"
    return 1
  fi
  if ! printf '%s\n' "$report3" | sed -n '/^== working tree$/,/^== history/p' | grep -q "notes.txt"; then
    echo "SELF-CHECK FAILED: the fault that lives only in the working tree was not named by the section that reads it"
    printf '%s\n' "$report3"
    return 1
  fi
  echo "self-check: the audit refuses a fault that lives only in the working tree"

  # A hit line is what gets quoted back at this file, so it must be the line `git grep` printed and
  # nothing else. `git grep <commit>` already carries the commit, and prefixing it again printed
  # `<sha>:<sha>:<path>...` -- a reader comparing this output with plain `git grep` would see a
  # prefix the tool never generated.
  if printf '%s\n' "$report2" | grep -Eq '^[0-9a-f]{40}:[0-9a-f]{40}:'; then
    echo "SELF-CHECK FAILED: a history hit printed the commit id twice"
    printf '%s\n' "$report2"
    return 1
  fi
  echo "self-check: a history hit carries the commit once, as git grep printed it"

  # A checkout git itself records as short. The walk succeeds, visits every commit the clone
  # has, and reports one -- a section that answered "clean" here would certify a history it
  # never saw, and the credential in the oldest commit is not in the clone to be found. The
  # credential is written first and deleted second, so the depth-1 clone has only its absence.
  local shallow status4 report4
  shallow=$(mktemp -d)
  mkdir -p "$shallow/origin"
  (
    cd "$shallow/origin"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf 'a key %s_%s\n' ghp "$(printf 'A%.0s' $(seq 1 40))" > gone.txt
    git add -A
    git commit -qm "a credential that will not stay"
    git rm -q gone.txt
    git commit -qm "and is gone from the tree"
  )
  git clone -q --depth 1 "file://$shallow/origin" "$shallow/clone"
  status4=0
  bash "$SELF" "$shallow/clone" > "$shallow/out.txt" 2>&1 || status4=$?
  report4=$(cat "$shallow/out.txt")
  rm -rf "$shallow"
  if [ "$status4" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a shallow checkout whose older commits it could not read"
    printf '%s\n' "$report4"
    return 1
  fi
  printf '%s' "$report4" | grep -q "this checkout is shallow" || {
    echo "SELF-CHECK FAILED: a shallow checkout was refused, but not for being shallow"
    printf '%s\n' "$report4"
    return 1; }
  echo "self-check: the audit refuses to certify a checkout whose history git records as short"

  # And the arm rests on that line and no other: with the shallowness branch cut out of a copy,
  # the same shallow clone is passed -- so the refusal above was the branch and not some accident
  # of the fixture. The copy is compared with the original first, so a substitution that did not
  # land fails the run instead of passing it.
  local cut status5 report5
  cut=$(mktemp -d)
  mkdir -p "$cut/tool" "$cut/origin"
  cp "$SELF" "$cut/tool/audit.sh"
  sed -i.bak 's#^  elif \[ "\$shallow" = true \]; then#  elif false; then#' "$cut/tool/audit.sh"
  if cmp -s "$SELF" "$cut/tool/audit.sh"; then
    echo "SELF-CHECK FAILED: the shallowness branch could not be cut out, so this arm measured nothing"
    rm -rf "$cut"
    return 1
  fi
  (
    cd "$cut/origin"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf 'a key %s_%s\n' ghp "$(printf 'B%.0s' $(seq 1 40))" > gone.txt
    git add -A
    git commit -qm "a credential that will not stay"
    git rm -q gone.txt
    git commit -qm "and is gone from the tree"
  )
  git clone -q --depth 1 "file://$cut/origin" "$cut/clone"
  status5=0
  bash "$cut/tool/audit.sh" "$cut/clone" > "$cut/out.txt" 2>&1 || status5=$?
  report5=$(cat "$cut/out.txt")
  rm -rf "$cut"
  if [ "$status5" -ne 0 ]; then
    echo "SELF-CHECK FAILED: with the shallowness branch cut out, a shallow clone was still refused"
    printf '%s\n' "$report5"
    return 1
  fi
  printf '%s' "$report5" | grep -q "^history: clean over the 1 commit(s) this checkout has$" || {
    echo "SELF-CHECK FAILED: the cut copy did not answer clean over the shallow clone it walked"
    printf '%s\n' "$report5"
    return 1; }
  echo "self-check: with the branch cut out, the same shallow clone is passed -- the arm is the line"

  # A list of the right length is not a list of the right commits. Here the enumeration is replaced
  # by a loop that writes HEAD's id as many times as the repository has commits. The list has the expected number of lines, every line reads
  # without error, and the credential -- which lives only in an older commit -- is never visited.
  # A section that prints a count of lines as a count of commits certifies a history it scanned
  # once; the count must be a count of distinct commits, and the refusal must say what it saw.
  local dup status6 report6
  dup=$(mktemp -d)
  mkdir -p "$dup/tool" "$dup/origin"
  cp "$SELF" "$dup/tool/audit.sh"
  sed -i.bak 's#^  git rev-list --all > "\$commit_list" || enum_rc=\$?#  for _i in $(seq 1 $(git rev-list --all --count)); do git rev-parse HEAD >> "$commit_list"; done#' "$dup/tool/audit.sh"
  if cmp -s "$SELF" "$dup/tool/audit.sh"; then
    echo "SELF-CHECK FAILED: the enumeration could not be replaced by a list of repeated ids, so this arm measured nothing"
    rm -rf "$dup"
    return 1
  fi
  (
    cd "$dup/origin"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf 'a key %s_%s\n' ghp "$(printf 'C%.0s' $(seq 1 40))" > gone.txt
    git add -A
    git commit -qm "a credential that will not stay"
    git rm -q gone.txt
    git commit -qm "and is gone from the tree"
    printf 'a note\n' > note.md
    git add -A
    git commit -qm "a third commit"
  )
  status6=0
  bash "$dup/tool/audit.sh" "$dup/origin" > "$dup/out.txt" 2>&1 || status6=$?
  report6=$(cat "$dup/out.txt")
  rm -rf "$dup"
  if [ "$status6" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a commit list of the expected length that never visits the finding"
    printf '%s\n' "$report6"
    return 1
  fi
  printf '%s' "$report6" | grep -q "named the same commit more than once" || {
    echo "SELF-CHECK FAILED: a commit list that repeats one id was refused, but not for repeating it"
    printf '%s\n' "$report6"
    return 1; }
  echo "self-check: the count the history section prints counts distinct commits, not lines"

  # A reader that breaks is not a reader that found nothing. Here the enumeration exits non-zero
  # without printing anything, so the section never ran; "clean" would be a sentence about nothing,
  # and a scanner whose whole history arm can go dead in silence is the scanner this file exists to
  # replace. The mutation is applied to a copy, and the copy is compared with the original first:
  # an arm that could not be planted measured nothing.
  local broken status3 report3
  broken=$(mktemp -d)
  mkdir -p "$broken/tool" "$broken/repo"
  cp "$SELF" "$broken/tool/audit.sh"
  sed -i.bak 's|^  git rev-list --all > "\$commit_list"|  false > "$commit_list"|' "$broken/tool/audit.sh"
  if cmp -s "$SELF" "$broken/tool/audit.sh"; then
    echo "SELF-CHECK FAILED: the history enumeration could not be replaced, so this arm measured nothing"
    rm -rf "$broken"
    return 1
  fi
  (
    cd "$broken/repo"
    git init -q .
    git config user.email a@b.c
    git config user.name a
    printf 'a note\n' > note.md
    git add -A
    git commit -qm "a first commit"
  )
  status3=0
  bash "$broken/tool/audit.sh" "$broken/repo" > "$broken/out.txt" 2>&1 || status3=$?
  report3=$(cat "$broken/out.txt")
  rm -rf "$broken"
  if [ "$status3" -eq 0 ]; then
    echo "SELF-CHECK FAILED: the audit passed a repository whose history it could not enumerate"
    printf '%s\n' "$report3"
    return 1
  fi
  printf '%s' "$report3" | grep -q "the reader could not answer" || {
    echo "SELF-CHECK FAILED: a failed enumeration was not named as a failed reader"
    printf '%s\n' "$report3"
    return 1; }
  echo "self-check: the audit refuses to call a section clean when its reader failed"
}

SELF=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
ROOT=$(cd "$(dirname "$0")/.." && pwd)

if [ "${1:-}" = "--self-check" ]; then
  self_check
else
  # A directory argument audits that repository instead of this one; the self-check uses it, and
  # nothing else should.
  cd "${1:-$ROOT}"
  audit
fi
