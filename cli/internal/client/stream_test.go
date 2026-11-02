package client_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/client"
	"github.com/ascorblack/daedalus/cli/internal/fakeserver"
)

func TestStreamResumesAfterADropWithoutLossOrRepeat(t *testing.T) {
	srv := fakeserver.New()
	defer srv.Close()
	srv.AddToken("dcl_test", "dev-1")
	release := make(chan struct{})
	srv.Turn = func(tt *fakeserver.Turn) {
		tt.Say("first part")
		<-release
		tt.Say("second part")
	}
	c := client.New(srv.URL, "dcl_test")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	ref, err := c.CreateSession(ctx, client.NewSession{Kind: "remote", Cwd: "/tmp/x"})
	if err != nil {
		t.Fatal(err)
	}
	stream := c.FollowSession(ctx, ref.ID, client.StreamOptions{MinBackoff: 20 * time.Millisecond, MaxBackoff: 50 * time.Millisecond, KeepaliveWindow: 2 * time.Second})
	defer stream.Close()
	next := func() client.StreamItem {
		t.Helper()
		select {
		case it, ok := <-stream.Items():
			if !ok {
				t.Fatal("the stream ended")
			}
			return it
		case <-ctx.Done():
			t.Fatal("timed out")
		}
		return client.StreamItem{}
	}
	if it := next(); it.Signal != client.SignalHello {
		t.Fatalf("first item: %+v", it)
	}
	if _, err := c.Send(ctx, ref.ID, client.SendBody{Text: "go"}); err != nil {
		t.Fatal(err)
	}
	seen := map[int64]int{}
	var text strings.Builder
	for {
		it := next()
		if it.Signal == client.SignalEvent && !it.Envelope.Ephemeral {
			seen[it.Envelope.Seq]++
		}
		if it.Signal == client.SignalEvent && it.Envelope.Kind == "message_stop" {
			break
		}
		if it.Signal == client.SignalEvent && it.Envelope.Kind == "content_block_delta" {
			text.WriteString(string(it.Envelope.Payload))
		}
	}
	srv.DropStreams()
	// The drop is reported, the stream comes back with a hello, and what happened meanwhile is
	// replayed from the cursor.
	if it := next(); it.Signal != client.SignalDropped {
		t.Fatalf("after the drop: %+v", it)
	}
	close(release)
	sawHello := false
	for {
		it := next()
		if it.Signal == client.SignalHello {
			sawHello = true
			continue
		}
		if it.Signal == client.SignalEvent && !it.Envelope.Ephemeral {
			seen[it.Envelope.Seq]++
		}
		if it.Signal == client.SignalEvent && it.Envelope.Kind == "run_settled" {
			break
		}
	}
	if !sawHello {
		t.Error("no hello after the reconnect")
	}
	for seq, n := range seen {
		if n != 1 {
			t.Errorf("event %d delivered %d times", seq, n)
		}
	}
	for seq := int64(1); seq <= int64(len(seen)); seq++ {
		if seen[seq] == 0 {
			t.Errorf("event %d was lost", seq)
		}
	}
}

func TestStreamRefusedCredentialIsFatal(t *testing.T) {
	srv := fakeserver.New()
	defer srv.Close()
	c := client.New(srv.URL, "dcl_wrong")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream := c.FollowSession(ctx, "s1", client.StreamOptions{})
	defer stream.Close()
	it := <-stream.Items()
	if it.Signal != client.SignalFatal || !client.IsUnauthorized(it.Err) {
		t.Fatalf("%+v", it)
	}
}
