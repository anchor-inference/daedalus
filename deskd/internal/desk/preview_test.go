package desk

import (
	"context"
	"errors"
	"image"
	"image/color"
	"io"
	"log/slog"
	"testing"

	"github.com/ascorblack/daedalus/deskd/internal/backend"
	"github.com/ascorblack/daedalus/deskd/internal/backend/fake"
)

// drawsWindows is the fake desktop with a window capture of its own, as the Windows backend has.
type drawsWindows struct {
	*fake.Fake
	fail  bool
	calls int
}

func (d *drawsWindows) CaptureWindow(ctx context.Context, w backend.Window) (*image.RGBA, error) {
	d.calls++
	if d.fail {
		return nil, errors.New("the window drew nothing")
	}
	img := image.NewRGBA(image.Rect(0, 0, 300, 100))
	for i := range img.Pix {
		img.Pix[i] = 255
	}
	img.SetRGBA(0, 0, color.RGBA{0, 0, 0, 255})
	return img, nil
}

func TestAWindowPreviewDrawsTheWindowByItselfWhereTheBackendCan(t *testing.T) {
	be := &drawsWindows{Fake: fake.New(fake.DefaultScene())}
	d := New(Config{}, be, nil, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	res, err := d.Preview(context.Background(), PreviewParams{Window: "10", MaxEdge: 150})
	if err != nil {
		t.Fatal(err)
	}
	// The window's own picture is 300×100; the screen region of window 10 would be 1600×1200.
	if be.calls != 1 || res.Width != 150 || res.Height != 50 {
		t.Fatalf("the window capture was not used: %d calls, %+v", be.calls, res)
	}
	// When it fails, the screen region it covers stands in.
	be.fail = true
	if res, err = d.Preview(context.Background(), PreviewParams{Window: "10", MaxEdge: 160}); err != nil || res.Width != 160 || res.Height != 120 {
		t.Fatalf("no fallback to the screen: %+v %v", res, err)
	}
	// A display preview never asks for a window capture.
	be.calls = 0
	if _, err := d.Preview(context.Background(), PreviewParams{Display: "2"}); err != nil || be.calls != 0 {
		t.Fatalf("display preview: %d calls, %v", be.calls, err)
	}
}
