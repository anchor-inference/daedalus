package sessions

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strconv"
	"time"
	"unicode/utf8"
)

// maxLine is the longest JSONL record read. Claude Code keeps a pasted screenshot inline, which is
// megabytes; a line beyond this is skipped and counted as damaged rather than held in memory.
const maxLine = 64 << 20

// eachLine streams the records of a JSONL file from an offset, calling fn with each line's offset
// and its bytes (without the newline; valid only during the call). It never holds more than one
// line. A last line without its newline is a record still being written by a live program: it is
// taken only when it is complete JSON, and otherwise left for the next read. consumed is the offset
// after the last record taken, which is where a later read continues.
func eachLine(f *os.File, from int64, fn func(off int64, line []byte) bool) (consumed int64, damaged int, err error) {
	if _, err := f.Seek(from, io.SeekStart); err != nil {
		return from, 0, err
	}
	r := bufio.NewReaderSize(f, 1<<20)
	off := from
	var long []byte
	var longBytes int64
	tooLong := false
	for {
		chunk, err := r.ReadSlice('\n')
		if errors.Is(err, bufio.ErrBufferFull) {
			longBytes += int64(len(chunk))
			if !tooLong && len(long)+len(chunk) <= maxLine {
				long = append(long, chunk...)
			} else {
				tooLong, long = true, nil // the bytes are dropped; only their count is kept
			}
			continue
		}
		line := chunk
		if long != nil {
			line = append(long, chunk...)
			long = nil
		}
		n := int64(len(line))
		if tooLong {
			n = longBytes + int64(len(chunk))
			tooLong, longBytes = false, 0
			damaged++
			off += n
			if err != nil {
				return off, damaged, nil
			}
			continue
		}
		longBytes = 0
		if err != nil {
			if !errors.Is(err, io.EOF) {
				return off, damaged, err
			}
			// The end: a record without its newline is taken only if it is whole.
			if t := bytes.TrimSpace(line); len(t) > 0 && t[0] == '{' && json.Valid(t) {
				fn(off, t)
				off += n
			}
			return off, damaged, nil
		}
		body := bytes.TrimRight(line, "\r\n")
		switch {
		case len(bytes.TrimSpace(body)) == 0:
		case body[0] != '{':
			damaged++
		default:
			if !fn(off, body) {
				return off + n, damaged, nil
			}
		}
		off += n
	}
}

// readSpan reads one record back by its offset and length.
func readSpan(f *os.File, s Span) ([]byte, error) {
	buf := make([]byte, s.Len)
	n, err := f.ReadAt(buf, s.Off)
	if n == s.Len {
		return buf, nil
	}
	if err == nil {
		err = io.ErrUnexpectedEOF
	}
	return nil, err
}

// fields picks the raw values of some top-level keys out of a JSON object without decoding the
// rest. A session file is hundreds of megabytes of tool output and pasted images; decoding every
// line into maps to learn its type and parent is what made a scan take minutes. Values come back as
// they are in the line (a string still quoted and escaped); a key not present gives nil. The
// returned slices point into line.
func fields(line []byte, keys ...string) [][]byte {
	out := make([][]byte, len(keys))
	i := skipSpace(line, 0)
	if i >= len(line) || line[i] != '{' {
		return out
	}
	i++
	for {
		i = skipSpace(line, i)
		if i >= len(line) || line[i] == '}' {
			return out
		}
		if line[i] == ',' {
			i++
			continue
		}
		if line[i] != '"' {
			return out
		}
		end := skipString(line, i)
		if end < 0 {
			return out
		}
		key := line[i+1 : end-1]
		i = skipSpace(line, end)
		if i >= len(line) || line[i] != ':' {
			return out
		}
		i = skipSpace(line, i+1)
		vend := skipValue(line, i)
		if vend < 0 {
			return out
		}
		for k, want := range keys {
			if out[k] == nil && string(key) == want {
				out[k] = line[i:vend]
			}
		}
		i = vend
	}
}

func skipSpace(b []byte, i int) int {
	for i < len(b) && (b[i] == ' ' || b[i] == '\t' || b[i] == '\n' || b[i] == '\r') {
		i++
	}
	return i
}

// skipString returns the index after the closing quote of the string starting at b[i].
func skipString(b []byte, i int) int {
	i++
	for {
		j := bytes.IndexByte(b[i:], '"')
		if j < 0 {
			return -1
		}
		i += j
		// The quote is escaped when an odd number of backslashes stands before it.
		bs := 0
		for k := i - 1; k >= 0 && b[k] == '\\'; k-- {
			bs++
		}
		i++
		if bs%2 == 0 {
			return i
		}
	}
}

// skipValue returns the index after the value starting at b[i].
func skipValue(b []byte, i int) int {
	if i >= len(b) {
		return -1
	}
	switch b[i] {
	case '"':
		return skipString(b, i)
	case '{', '[':
		depth := 0
		for i < len(b) {
			switch b[i] {
			case '"':
				i = skipString(b, i)
				if i < 0 {
					return -1
				}
				continue
			case '{', '[':
				depth++
			case '}', ']':
				depth--
				if depth == 0 {
					return i + 1
				}
			}
			i++
		}
		return -1
	default:
		for i < len(b) && b[i] != ',' && b[i] != '}' && b[i] != ']' && b[i] != ' ' && b[i] != '\n' {
			i++
		}
		return i
	}
}

// str decodes a raw JSON string value; anything else gives "".
func str(raw []byte) string {
	if len(raw) < 2 || raw[0] != '"' {
		return ""
	}
	inner := raw[1 : len(raw)-1]
	if bytes.IndexByte(inner, '\\') < 0 && utf8.Valid(inner) {
		return string(inner)
	}
	var s string
	if json.Unmarshal(raw, &s) != nil {
		return ""
	}
	return s
}

// strPrefix decodes at most the first n bytes' worth of a raw JSON string: enough to tell a tag of
// the CLI's own from an operator's words without unescaping a megabyte of pasted text.
func strPrefix(raw []byte, n int) string {
	if len(raw) < 2 || raw[0] != '"' {
		return ""
	}
	if len(raw) <= n+2 {
		return str(raw)
	}
	cut := raw[:n+1]
	// Do not cut inside an escape: back up to before a trailing backslash run.
	for len(cut) > 1 && cut[len(cut)-1] == '\\' {
		cut = cut[:len(cut)-1]
	}
	var s string
	if json.Unmarshal(append(append([]byte{}, cut...), '"'), &s) != nil {
		// A cut inside \uXXXX: shorter until it decodes.
		for k := 1; k < 6 && len(cut) > k; k++ {
			if json.Unmarshal(append(append([]byte{}, cut[:len(cut)-k]...), '"'), &s) == nil {
				return s
			}
		}
		return ""
	}
	return s
}

func boolv(raw []byte) bool { return string(raw) == "true" }

func intv(raw []byte) int64 {
	n, _ := strconv.ParseInt(string(raw), 10, 64)
	return n
}

// stamp parses the times the programs write: RFC 3339 with or without fractions.
func stamp(text string) time.Time {
	if text == "" {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339Nano, text)
	if err != nil {
		return time.Time{}
	}
	return t.UTC()
}

// clip shortens a text to at most n bytes on a rune boundary.
func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
