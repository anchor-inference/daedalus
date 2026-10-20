package relay

import (
	"errors"
	"fmt"
)

// A relay frame travels on its own channel of the daemon's socket (relay.open names it), but one
// socket frame holds at most 1 MiB and a relay frame from the extension may be far larger: a
// full-page screenshot comes back in one `result`, and Chrome lets an extension send its native host
// up to 64 MiB. So every socket frame on the relay channel starts with one byte saying whether the
// relay frame ends in it (PieceLast) or goes on in the next one (PieceMore). A frame that fits is
// one piece; the bridge and the host's WebSocket proxy split and join exactly as the daemon does.
const (
	PieceLast byte = 0
	PieceMore byte = 1
)

// MaxFromExtension bounds one relay frame from the extension: Chrome's own limit on what an
// extension sends its native host. A larger one is a broken or hostile peer, and the relay ends.
const MaxFromExtension = 64 << 20

// MaxToExtension bounds one relay frame to the extension: Chrome's limit on a native host's
// message is 1 MB, and a larger one makes Chrome drop the host. The daemon refuses such a command
// itself rather than lose the relay over it.
const MaxToExtension = 1 << 20

// ErrTooLarge is a relay frame past its direction's bound.
var ErrTooLarge = errors.New("the relay frame is larger than the relay carries")

// Split cuts one relay frame into the payloads of socket frames of at most max bytes each, the
// marker byte included.
func Split(frame []byte, max int) [][]byte {
	room := max - 1
	if room < 1 {
		panic("relay.Split: no room for a piece")
	}
	var out [][]byte
	for len(frame) > room {
		p := make([]byte, 0, room+1)
		p = append(p, PieceMore)
		p = append(p, frame[:room]...)
		out = append(out, p)
		frame = frame[room:]
	}
	p := make([]byte, 0, len(frame)+1)
	p = append(p, PieceLast)
	return append(out, append(p, frame...))
}

// Joiner puts relay frames back together from socket-frame payloads.
type Joiner struct {
	// Max bounds one joined frame.
	Max int
	buf []byte
}

// Add takes one payload. It returns the whole relay frame once its last piece is in, nil while
// more is to come, and an error for a payload that is not a piece or a frame past Max.
func (j *Joiner) Add(payload []byte) ([]byte, error) {
	if len(payload) == 0 {
		return nil, errors.New("an empty piece")
	}
	switch payload[0] {
	case PieceMore, PieceLast:
	default:
		return nil, fmt.Errorf("a piece marked %d, not 0 or 1", payload[0])
	}
	if j.Max > 0 && len(j.buf)+len(payload)-1 > j.Max {
		j.buf = nil
		return nil, ErrTooLarge
	}
	if payload[0] == PieceMore {
		j.buf = append(j.buf, payload[1:]...)
		return nil, nil
	}
	if j.buf == nil {
		return append([]byte(nil), payload[1:]...), nil
	}
	out := append(j.buf, payload[1:]...)
	j.buf = nil
	return out, nil
}
