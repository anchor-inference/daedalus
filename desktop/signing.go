package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

// Release signing. SHA256SUMS proves a download is the file a release lists; it
// cannot prove who published the release. A signature over SHA256SUMS by a key whose public half is
// compiled into the launcher can. The format is OpenBSD signify's — plain Ed25519, which the Go
// standard library verifies, and which signify, minisign -l (legacy) and a few lines of any language
// produce:
//
//	SHA256SUMS.sig:  "untrusted comment: …\n" + base64("Ed" | key id (8 bytes) | signature (64 bytes)) + "\n"
//	public key:      "untrusted comment: …\n" + base64("Ed" | key id (8 bytes) | public key (32 bytes)) + "\n"
//
// trustedReleaseKeys is empty on purpose: which key, who holds its private half and where (a CI
// secret, a hardware token, an offline machine), and how it is rotated are the operator's decisions
// (desktop/SIGNING.md), and no key is made up here. While it is empty, a release is not required to
// be signed — which is exactly the state in which publishing to users is a no-go. Once it holds a key, a
// release without a valid SHA256SUMS.sig by one of them is refused, before anything is unpacked.
var trustedReleaseKeys []string

// A fixture build can inject a disposable public key with -ldflags -X without modifying the
// source tree. Normal builds leave this empty; the operator still chooses the production key.
var linkedFixtureReleaseKey string

func init() {
	if linkedFixtureReleaseKey != "" {
		trustedReleaseKeys = append(trustedReleaseKeys, linkedFixtureReleaseKey)
	}
}

const signatureAsset = "SHA256SUMS.sig"

// releaseMessage is what a release's signature covers: the tag and the checksums together. Signing
// SHA256SUMS alone would let an old signed release be published again under a newer tag — the
// signature would still hold, and the launcher would install old code as an upgrade. With the tag
// inside, a signature is good for exactly the release it was made for.
//
//	daedalus-release <tag>\n<SHA256SUMS exactly as published>
func releaseMessage(tag string, sums []byte) []byte {
	return append([]byte("daedalus-release "+tag+"\n"), sums...)
}

type signifyKey struct {
	id  [8]byte
	pub ed25519.PublicKey
}

// decodeSignify reads the base64 line of a signify file and checks its algorithm and length.
func decodeSignify(text string, bodyLen int) ([8]byte, []byte, error) {
	var id [8]byte
	line := ""
	for _, l := range strings.Split(strings.TrimSpace(text), "\n") {
		l = strings.TrimSpace(l)
		if l == "" || strings.HasPrefix(l, "untrusted comment:") {
			continue
		}
		line = l
		break
	}
	raw, err := base64.StdEncoding.DecodeString(line)
	if err != nil {
		return id, nil, fmt.Errorf("not base64: %w", err)
	}
	if len(raw) != 2+8+bodyLen || !bytes.Equal(raw[:2], []byte("Ed")) {
		return id, nil, errors.New("not an Ed25519 signify record")
	}
	copy(id[:], raw[2:10])
	return id, raw[10:], nil
}

func parsePublicKey(text string) (signifyKey, error) {
	id, body, err := decodeSignify(text, ed25519.PublicKeySize)
	if err != nil {
		return signifyKey{}, fmt.Errorf("public key: %w", err)
	}
	return signifyKey{id: id, pub: ed25519.PublicKey(body)}, nil
}

// verifyRelease checks sig over message against keys. With no keys it answers nil and reports that
// nothing was checked.
func verifyRelease(message, sig []byte, keys []string) (checked bool, err error) {
	if len(keys) == 0 {
		return false, nil
	}
	if len(sig) == 0 {
		return true, fmt.Errorf("the release has no %s, and this launcher only installs signed releases", signatureAsset)
	}
	id, body, err := decodeSignify(string(sig), ed25519.SignatureSize)
	if err != nil {
		return true, fmt.Errorf("%s: %w", signatureAsset, err)
	}
	for _, text := range keys {
		key, err := parsePublicKey(text)
		if err != nil {
			return true, err
		}
		if key.id == id {
			if ed25519.Verify(key.pub, message, body) {
				return true, nil
			}
			return true, fmt.Errorf("%s does not verify with the release key", signatureAsset)
		}
	}
	return true, fmt.Errorf("%s is signed by a key this launcher does not trust", signatureAsset)
}
