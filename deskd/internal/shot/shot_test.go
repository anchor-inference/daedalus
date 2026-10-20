package shot

import (
	"bytes"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"testing"
)

func fill(img *image.RGBA, r image.Rectangle, c color.RGBA) {
	for y := r.Min.Y; y < r.Max.Y; y++ {
		for x := r.Min.X; x < r.Max.X; x++ {
			img.SetRGBA(x, y, c)
		}
	}
}

func TestDownscaleAveragesWhatEachPixelCovers(t *testing.T) {
	src := image.NewRGBA(image.Rect(0, 0, 4, 2))
	fill(src, image.Rect(0, 0, 2, 2), color.RGBA{200, 0, 0, 255})
	fill(src, image.Rect(2, 0, 4, 2), color.RGBA{0, 0, 100, 255})
	got := Downscale(src, 2, 1)
	if c := got.RGBAAt(0, 0); c != (color.RGBA{200, 0, 0, 255}) {
		t.Errorf("left %v", c)
	}
	if c := got.RGBAAt(1, 0); c != (color.RGBA{0, 0, 100, 255}) {
		t.Errorf("right %v", c)
	}
	// A third: the middle target pixel covers half a red and half a blue source pixel.
	src = image.NewRGBA(image.Rect(0, 0, 3, 1))
	fill(src, image.Rect(0, 0, 1, 1), color.RGBA{255, 0, 0, 255})
	fill(src, image.Rect(1, 0, 2, 1), color.RGBA{0, 0, 0, 255})
	fill(src, image.Rect(2, 0, 3, 1), color.RGBA{0, 0, 255, 255})
	got = Downscale(src, 2, 1)
	if c := got.RGBAAt(0, 0); c.R != 170 || c.B != 0 {
		t.Errorf("a pixel and a half: %v", c)
	}
}

func TestAOnePixelLineSurvivesTheDownscale(t *testing.T) {
	src := image.NewRGBA(image.Rect(0, 0, 2880, 1800))
	fill(src, src.Bounds(), color.RGBA{255, 255, 255, 255})
	fill(src, image.Rect(1001, 0, 1002, 1800), color.RGBA{0, 0, 0, 255})
	got := Downscale(src, 1568, 980)
	darkest := 255
	for x := 0; x < 1568; x++ {
		darkest = min(darkest, int(got.RGBAAt(x, 500).R))
	}
	if darkest > 200 {
		t.Errorf("the line faded to %d", darkest)
	}
}

func TestNeverScalesUp(t *testing.T) {
	src := image.NewRGBA(image.Rect(0, 0, 10, 10))
	if Downscale(src, 20, 20) != src {
		t.Error("scaled up")
	}
}

func TestEncodings(t *testing.T) {
	img := image.NewRGBA(image.Rect(0, 0, 8, 8)) // alpha zero everywhere, as some captures leave it
	fill(img, img.Bounds(), color.RGBA{0, 200, 0, 0})
	b, err := Encode(img, JPEG, 90)
	if err != nil {
		t.Fatal(err)
	}
	dec, err := jpeg.Decode(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if _, g, _, _ := dec.At(4, 4).RGBA(); g>>8 < 150 {
		t.Errorf("a zero alpha came out dark: green %d", g>>8)
	}
	if b, err = Encode(img, PNG, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := png.Decode(bytes.NewReader(b)); err != nil {
		t.Fatal(err)
	}
	if _, err := Encode(img, "gif", 0); err == nil {
		t.Error("gif encoded")
	}
	if _, err := Encode(img, JPEG, 0); err == nil {
		t.Error("quality 0 accepted")
	}
}
