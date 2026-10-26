package view

import (
	"bytes"
	"image"
	"image/color"
	"image/jpeg"
	"sync"
	"testing"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

// sink stands in for the connection: it keeps what a client sends.
type sink struct {
	mu     sync.Mutex
	frames []wire.Frame
	closed bool
}

func (s *sink) SendChannel(_ uint32, b []byte) error {
	f, err := wire.Decode(b)
	if err == nil && f.Type == wire.TypeFrame {
		s.mu.Lock()
		s.frames = append(s.frames, f)
		s.mu.Unlock()
	}
	return nil
}

func (s *sink) CloseChannel(uint32) { s.closed = true }

func (s *sink) got() []wire.Frame {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]wire.Frame(nil), s.frames...)
}

func testJPEG(t *testing.T, w, h int, shade uint8) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.Set(x, y, color.RGBA{shade, uint8(x), uint8(y), 255})
		}
	}
	var b bytes.Buffer
	if err := jpeg.Encode(&b, img, &jpeg.Options{Quality: 60}); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func pic(t *testing.T, n uint64, w, h int) *picture {
	return &picture{n: n, jpeg: testJPEG(t, w, h, uint8(n)), meta: wire.Meta{W: w, H: h, VW: float64(w), VH: float64(h), TS: int64(n)}}
}

func client(tier string, fps int) (*Client, *sink) {
	s := &sink{}
	h := &Hub{fps: fps}
	return &Client{id: "c1", hub: h, conn: s, tier: tier, quality: 60, adaptive: true, tab: nil, quit: make(chan struct{})}, s
}

func TestNewestWinsAndOneFrameInFlight(t *testing.T) {
	cl, s := client("live", 1000) // no pacing: flow control alone
	cl.tab = testTab()
	cl.offer(pic(t, 1, 64, 64))
	cl.offer(pic(t, 2, 64, 64))
	cl.offer(pic(t, 3, 64, 64))
	if got := s.got(); len(got) != 1 || got[0].FrameNo != 1 || got[0].Meta.TS != 1 {
		t.Fatalf("before an acknowledgement: %+v", got)
	}
	cl.ack(1)
	time.Sleep(20 * time.Millisecond)
	got := s.got()
	if len(got) != 2 || got[1].FrameNo != 2 || got[1].Meta.TS != 3 {
		t.Fatalf("after one: %+v", got)
	}
	// An acknowledgement of a frame never sent changes nothing.
	cl.ack(7)
	cl.offer(pic(t, 4, 64, 64))
	if n := len(s.got()); n != 2 {
		t.Fatalf("%d frames with one in flight", n)
	}
	cl.ack(2)
	time.Sleep(20 * time.Millisecond)
	if got := s.got(); len(got) != 3 || got[2].Meta.TS != 4 {
		t.Fatalf("after two: %+v", got)
	}
}

func TestPaceOfALiveClient(t *testing.T) {
	cl, s := client("live", 10) // 100 ms between frames
	cl.tab = testTab()
	cl.offer(pic(t, 1, 64, 64))
	cl.ack(1)
	cl.offer(pic(t, 2, 64, 64))
	if n := len(s.got()); n != 1 {
		t.Fatalf("a second frame within the pace: %d", n)
	}
	time.Sleep(150 * time.Millisecond)
	if n := len(s.got()); n != 2 {
		t.Fatalf("the paced frame did not follow: %d", n)
	}
}

func TestThumbnailsAreSmallAndOnlyWhenChanged(t *testing.T) {
	cl, s := client("thumb", 15)
	cl.tab = testTab()
	p := pic(t, 1, 1280, 800)
	cl.offer(p)
	got := s.got()
	if len(got) != 1 || got[0].Meta.W != 320 || got[0].Meta.H != 200 || got[0].Meta.Tier != "thumb" {
		t.Fatalf("thumbnail: %+v", got)
	}
	if img, err := jpeg.DecodeConfig(bytes.NewReader(got[0].Image)); err != nil || img.Width != 320 {
		t.Fatalf("thumbnail image: %+v %v", img, err)
	}
	cl.ack(1)
	cl.mu.Lock()
	cl.sentAt = time.Time{}
	cl.mu.Unlock()
	cl.offer(p) // the same picture again: nothing new to show
	if n := len(s.got()); n != 1 {
		t.Fatalf("an unchanged thumbnail was sent again: %d", n)
	}
}

func TestDegradedClientGetsHalfSize(t *testing.T) {
	cl, s := client("live", 1000)
	cl.tab = testTab()
	for i := 1; i <= 5; i++ {
		cl.offer(pic(t, uint64(i), 640, 400))
		cl.mu.Lock()
		cl.sentAt = time.Now().Add(-time.Second) // an acknowledgement a second late
		cl.mu.Unlock()
		cl.ack(uint32(i))
	}
	cl.offer(pic(t, 6, 640, 400))
	got := s.got()
	last := got[len(got)-1]
	if last.Meta.W != 320 || last.Meta.H != 200 {
		t.Fatalf("a slow client's frame: %+v", last.Meta)
	}
}

// slowAcks makes the client's link look slow for five frames: each acknowledged a second late.
func slowAcks(t *testing.T, cl *Client, from int) {
	for i := from; i < from+5; i++ {
		cl.offer(pic(t, uint64(i), 640, 400))
		cl.mu.Lock()
		cl.sentAt = time.Now().Add(-time.Second)
		cl.mu.Unlock()
		cl.ack(cl.inFlight)
	}
}

// TestADegradedPictureIsSharpenedOnceThePageIsStill holds the fix for a picture that stayed half size
// at quality 40: a still page sends no more frames, so the last degraded one is sent again whole.
func TestADegradedPictureIsSharpenedOnceThePageIsStill(t *testing.T) {
	cl, s := client("live", 1000)
	cl.tab = testTab()
	cl.hub.casts = map[*browser.Tab]*cast{}
	c := &cast{hub: cl.hub, tab: cl.tab, clients: map[*Client]bool{cl: true}}
	cl.hub.casts[cl.tab] = c
	slowAcks(t, cl, 1)
	last := pic(t, 6, 640, 400)
	c.newest = last
	cl.offer(last)
	got := s.got()
	if got[len(got)-1].Meta.W != 320 {
		t.Fatalf("not degraded: %+v", got[len(got)-1].Meta)
	}
	cl.ack(cl.inFlight)
	time.Sleep(sharpenAfter + 200*time.Millisecond)
	got = s.got()
	whole := got[len(got)-1]
	if whole.Meta.W != 640 || whole.Meta.H != 400 || whole.Meta.TS != 6 || whole.FrameNo != got[len(got)-2].FrameNo+1 {
		t.Fatalf("the still page's picture was not sent whole: %+v", whole.Meta)
	}
	// A page still moving is not interrupted: a newer picture first, no sharpening of the old one.
	cl.ack(cl.inFlight)
	n := len(s.got())
	cl.offer(pic(t, 7, 640, 400))
	cl.ack(cl.inFlight)
	cl.offer(pic(t, 8, 640, 400))
	if len(s.got()) != n+2 {
		t.Fatalf("frames while moving: %d after %d", len(s.got()), n)
	}
}

func TestASharpClientIsNeverDegradedAndKeepsItsOwnPace(t *testing.T) {
	cl, s := client("live", 1000)
	cl.tab = testTab()
	cl.adaptive = false
	slowAcks(t, cl, 1)
	cl.offer(pic(t, 6, 640, 400))
	time.Sleep(20 * time.Millisecond)
	got := s.got()
	if last := got[len(got)-1]; last.Meta.W != 640 || last.Meta.TS != 6 {
		t.Fatalf("a client that asked for no adaptation was degraded: %+v", last.Meta)
	}
	// Its own pace, below the daemon's cap.
	cl.mu.Lock()
	cl.fps = 5
	cl.mu.Unlock()
	cl.ack(cl.inFlight)
	next := pic(t, 7, 640, 400) // made first: encoding it under the race detector takes a while
	cl.mu.Lock()
	cl.sentAt = time.Now()
	cl.mu.Unlock()
	n := len(s.got())
	cl.offer(next)
	time.Sleep(100 * time.Millisecond)
	if len(s.got()) != n {
		t.Fatal("a frame within the client's own pace of 5 a second")
	}
	time.Sleep(200 * time.Millisecond)
	if len(s.got()) != n+1 {
		t.Fatal("the paced frame did not follow")
	}
}

func TestScaleFits(t *testing.T) {
	for _, c := range []struct{ w, h, mw, mh, ww, wh int }{
		{1280, 800, 320, 200, 320, 200},
		{1280, 400, 320, 200, 320, 100},
		{400, 1280, 320, 200, 62, 200},
		{200, 100, 320, 200, 200, 100},
	} {
		if w, h := fit(c.w, c.h, c.mw, c.mh); w != c.ww || h != c.wh {
			t.Errorf("fit %dx%d in %dx%d = %dx%d", c.w, c.h, c.mw, c.mh, w, h)
		}
	}
	out, w, h, err := Scale(testJPEG(t, 1280, 800, 9), 320, 200, 45)
	if err != nil || w != 320 || h != 200 || len(out) == 0 {
		t.Fatalf("scale: %dx%d %v", w, h, err)
	}
}
