package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"testing"
)

// A key made for the test and thrown away with it; no key of the project's is in the repository.
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
	// With no key compiled in, nothing is checked — and that is said, not hidden.
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
	newRelease().serve(t, "desktop-v0.13.0")
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
