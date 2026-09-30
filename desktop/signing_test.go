package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The project's key, as the build compiled it in, before the tests put their own in its place.
var compiledReleaseKeys = trustedReleaseKeys

// testReleaseKey and testReleaseSign are the key every release the tests publish is signed with.
// The tests of the upgrade machinery run exactly as a release build does — a release without a
// good SHA256SUMS.sig is refused — only with a key they made for themselves, since the project's
// signs nothing outside the operator's machine. useTestReleaseKey puts it in place in TestMain.
var (
	testReleaseKey  string
	testReleaseSign func([]byte) []byte
)

func useTestReleaseKey() {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		panic(err)
	}
	record := func(body []byte) string {
		return "untrusted comment: test\n" + base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), []byte("testkey1")...), body...)) + "\n"
	}
	testReleaseKey = record(pub)
	testReleaseSign = func(message []byte) []byte { return []byte(record(ed25519.Sign(priv, message))) }
	trustedReleaseKeys = []string{testReleaseKey}
}

// A key made for the test and thrown away with it.
func testKey(t *testing.T, id string) (string, func([]byte) []byte) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	record := func(body []byte) string {
		return "untrusted comment: test\n" + base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), []byte(id)...), body...)) + "\n"
	}
	return record(pub), func(message []byte) []byte { return []byte(record(ed25519.Sign(priv, message))) }
}

func TestAReleaseSignedByTheTrustedKeyVerifies(t *testing.T) {
	sums := []byte("0123  daedalus-desktop-linux-amd64.tar.gz\n")
	key, sign := testKey(t, "key00001")
	other, signOther := testKey(t, "key00002")
	_ = other
	if checked, err := verifyRelease(sums, sign(sums), []string{key}); !checked || err != nil {
		t.Fatalf("a good signature: %v %v", checked, err)
	}
	for name, sig := range map[string][]byte{
		"no signature":       nil,
		"another key":        signOther(sums),
		"another message":    sign([]byte("other")),
		"garbage":            []byte("untrusted comment: x\nnot base64!\n"),
		"a truncated record": []byte("untrusted comment: x\nRWQ=\n"),
	} {
		if _, err := verifyRelease(sums, sig, []string{key}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// With no key at all, nothing is checked — and that is said, not hidden. A build always has one.
	if checked, err := verifyRelease(sums, nil, nil); checked || err != nil {
		t.Fatalf("no keys: %v %v", checked, err)
	}
}

func TestWithAReleaseKeyAnUnsignedReleaseIsRefusedBeforeStaging(t *testing.T) {
	key, _ := testKey(t, "key00001")
	old := trustedReleaseKeys
	trustedReleaseKeys = []string{key}
	t.Cleanup(func() { trustedReleaseKeys = old })
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	release := newRelease()
	release.unsigned = true
	release.serve(t, "desktop-v0.13.0")
	// The release has no SHA256SUMS.sig: it is not offered, and that is said — not "newest".
	u := in.upgrader("yes\n", true)
	err := u.Run(context.Background())
	if !errors.Is(err, errUnsignedRelease) || !strings.Contains(err.Error(), "desktop-v0.13.0 is published but not signed") {
		t.Fatalf("err = %v", err)
	}
	if strings.Contains(fmt.Sprint(u.out), "newest") {
		t.Fatalf("an unsigned newer release was reported as nothing newer:\n%s", u.out)
	}
	if in.file(t, launcherName()) != "old launcher" || exists(backupsDir(in.paths)) {
		t.Fatal("an unsigned release was installed under a release key")
	}
}

// The project's release key is compiled in, and it is the key minisign names A18524FA935353DF.
func TestTheProjectsReleaseKeyIsCompiledIn(t *testing.T) {
	if len(compiledReleaseKeys) != 1 {
		t.Fatalf("the build trusts %d release keys, want the project's one", len(compiledReleaseKeys))
	}
	key, err := parsePublicKey(compiledReleaseKeys[0])
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprintf("%x", key.id[:]); got != "df535393fa2485a1" {
		t.Fatalf("the compiled key's id is %s", got)
	}
	// A signature in the right format by another key naming that id is a forgery, and refused; the
	// same signature verifies against the key that made it, so the refusal is about the key.
	forger, sign := testKey(t, string(key.id[:]))
	message := releaseMessage("desktop-v0.13.0", []byte("0123  daedalus-desktop-linux-amd64.tar.gz\n"))
	if _, err := verifyRelease(message, sign(message), compiledReleaseKeys); err == nil {
		t.Fatal("a signature by another key was accepted for the project's key id")
	}
	if checked, err := verifyRelease(message, sign(message), []string{forger}); !checked || err != nil {
		t.Fatalf("the same signature does not verify against the key that made it: %v", err)
	}
	// A signature by a key with another id is refused as a key this launcher does not trust.
	_, other := testKey(t, "key00009")
	if _, err := verifyRelease(message, other(message), compiledReleaseKeys); err == nil || !strings.Contains(err.Error(), "does not trust") {
		t.Fatalf("a foreign key: %v", err)
	}
}

// What minisign -S -l (legacy, not prehashed) writes, made with a throwaway key by minisign 0.11:
// after the signature line come a trusted comment and a second signature over it. The launcher reads
// the first line that is not the untrusted comment and verifies it over the message as it is.
func TestAMinisignLegacySignatureVerifies(t *testing.T) {
	read := func(name string) []byte {
		body, err := os.ReadFile(filepath.Join("testdata", "minisign", name))
		if err != nil {
			t.Fatal(err)
		}
		return body
	}
	message, sig, pub := read("message"), read("message.minisig"), read("throwaway.pub")
	if !strings.Contains(string(sig), "trusted comment:") {
		t.Fatal("the fixture is not minisign's format")
	}
	if checked, err := verifyRelease(message, sig, []string{string(pub)}); !checked || err != nil {
		t.Fatalf("minisign's signature: %v", err)
	}
	if _, err := verifyRelease(append(message, '!'), sig, []string{string(pub)}); err == nil {
		t.Fatal("minisign's signature verified another message")
	}
}
