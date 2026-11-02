package client

import (
	"io"
	"strings"
	"testing"
)

func TestSSEReaderLineEndsCommentsAndData(t *testing.T) {
	in := ": keepalive\r\n\r\nid: 7\revent: tool_result\ndata: {\"a\":\ndata: 1}\r\n\r\nevent: half\ndata: lost"
	r := newSSEReader(strings.NewReader(in))
	f, err := r.next()
	if err != nil || !f.isComment || f.comment != "keepalive" {
		t.Fatalf("comment: %+v %v", f, err)
	}
	f, err = r.next()
	if err != nil || f.event != "tool_result" || f.id != "7" || f.data != "{\"a\":\n1}" {
		t.Fatalf("event: %+v %v", f, err)
	}
	// An event without its blank line was never dispatched.
	if _, err := r.next(); err != io.EOF {
		t.Fatalf("a half event was dispatched: %v", err)
	}
}
