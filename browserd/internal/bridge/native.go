// Package bridge is `browserd bridge`, Chrome's native messaging host for the Daedalus extension.
// Chrome starts it when the extension connects, with the extension's origin as its argument, and
// talks to it over stdin and stdout; the bridge checks the origin against the pinned extension ids,
// connects to the `user` daemon (--backend attach) on this machine as its relay, and carries the
// extension's frames to the daemon and back unchanged. It never listens on anything: a web page has
// no way to reach it, and Chrome starts it only for an extension its manifest allows.
package bridge

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"

	"github.com/ascorblack/daedalus/browserd/internal/relay"
)

// Native messaging is a 4-byte length in the machine's byte order, which is little-endian on every
// platform Chrome runs on, then that many bytes of UTF-8 JSON. Chrome caps a message from the host
// at 1 MB (and drops a host that sends more) and a message to it at 64 MiB.

// ReadMessage reads one message from Chrome. A length past relay.MaxFromExtension is refused before
// anything is allocated for it.
func ReadMessage(r io.Reader) ([]byte, error) {
	var head [4]byte
	if _, err := io.ReadFull(r, head[:]); err != nil {
		return nil, err
	}
	n := binary.LittleEndian.Uint32(head[:])
	if n > relay.MaxFromExtension {
		return nil, fmt.Errorf("%w: a message of %d bytes from Chrome", relay.ErrTooLarge, n)
	}
	b := make([]byte, n)
	if _, err := io.ReadFull(r, b); err != nil {
		if errors.Is(err, io.EOF) {
			err = io.ErrUnexpectedEOF
		}
		return nil, err
	}
	return b, nil
}

// WriteMessage writes one message to Chrome, in one Write. A message past Chrome's 1 MB is refused
// here: sent, it would cost the extension its connection.
func WriteMessage(w io.Writer, b []byte) error {
	if len(b) > relay.MaxToExtension {
		return fmt.Errorf("%w: a message of %d bytes to Chrome", relay.ErrTooLarge, len(b))
	}
	out := make([]byte, 4, 4+len(b))
	binary.LittleEndian.PutUint32(out, uint32(len(b)))
	_, err := w.Write(append(out, b...))
	return err
}
