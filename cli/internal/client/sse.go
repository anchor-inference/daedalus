package client

import (
	"bufio"
	"bytes"
	"io"
	"strings"
)

// maxFrameBytes bounds one SSE line. A tool result can carry a whole file in one envelope, so the
// scanner's 64 KiB default would stop a healthy stream; past this the frame is fatal rather than
// retried, because the same frame would be replayed on every reconnect.
const maxFrameBytes = 8 << 20

// frame is one thing read off the wire: a dispatched event, or a comment. Comments are surfaced,
// because the server's liveness signal is one (`: keepalive`), and a reader that only sees events
// cannot tell a quiet stream from a dead one.
type frame struct {
	comment   string
	isComment bool
	event     string
	data      string
	id        string
	hasID     bool
}

// sseReader turns a byte stream into frames.
type sseReader struct {
	scanner  *bufio.Scanner
	pending  frame
	building bool
}

func newSSEReader(r io.Reader) *sseReader {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64<<10), maxFrameBytes)
	scanner.Split(scanSSELines)
	return &sseReader{scanner: scanner}
}

// next returns the next frame, or io.EOF when the stream ends. A half-built event at the end is
// dropped: without its blank line it was never dispatched, and the reconnect replays it from the
// last cursor anyway.
func (r *sseReader) next() (frame, error) {
	for r.scanner.Scan() {
		line := r.scanner.Text()
		if line == "" {
			if !r.building {
				continue
			}
			out := r.pending
			r.pending, r.building = frame{}, false
			return out, nil
		}
		if strings.HasPrefix(line, ":") {
			return frame{isComment: true, comment: strings.TrimPrefix(strings.TrimPrefix(line, ":"), " ")}, nil
		}
		field, value := splitField(line)
		switch field {
		case "event":
			r.pending.event = value
			r.building = true
		case "data":
			// Multi-line data joins with newlines, which is what keeps a JSON body with a newline
			// in it intact.
			if r.building && r.pending.data != "" {
				r.pending.data += "\n" + value
			} else {
				r.pending.data = value
			}
			r.building = true
		case "id":
			// The id is the reconnect cursor; sending back something the server did not issue
			// would fail the next connect, and the specification ignores an id with a NUL.
			if !strings.ContainsRune(value, 0) {
				r.pending.id, r.pending.hasID = value, true
			}
			r.building = true
		}
	}
	if err := r.scanner.Err(); err != nil {
		return frame{}, err
	}
	return frame{}, io.EOF
}

// splitField splits `field: value`, stripping exactly one leading space as the specification says.
func splitField(line string) (string, string) {
	idx := strings.IndexByte(line, ':')
	if idx < 0 {
		return line, ""
	}
	return line[:idx], strings.TrimPrefix(line[idx+1:], " ")
}

// scanSSELines splits on any of the three line ends the specification allows. bufio.ScanLines would
// leave a bare CR inside a token and turn a field name into one nothing matches.
func scanSSELines(data []byte, atEOF bool) (int, []byte, error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	if i := bytes.IndexAny(data, "\r\n"); i >= 0 {
		if data[i] == '\n' {
			return i + 1, data[:i], nil
		}
		// A CR at the end of the buffer may be half of a CRLF: wait for more rather than strand
		// the LF as an empty line that dispatches the event early.
		if i == len(data)-1 && !atEOF {
			return 0, nil, nil
		}
		if i+1 < len(data) && data[i+1] == '\n' {
			return i + 2, data[:i], nil
		}
		return i + 1, data[:i], nil
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}
