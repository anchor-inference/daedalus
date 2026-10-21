// Package shot turns a capture into the picture the model gets: scaled down to the size the daemon
// fixed for it and encoded. The daemon scales, not the model's provider: a provider that resizes an
// oversized picture on its side shifts every coordinate read from it, which is how clicks drifted by
// a seventh of the screen on a 16:10 display in one vendor's own sample.
package shot

import (
	"bytes"
	"fmt"
	"image"
	"image/jpeg"
	"image/png"
)

// The formats a screenshot may be encoded in.
const (
	PNG  = "png"
	JPEG = "jpeg"
)

// Downscale returns src scaled to w×h by averaging every source pixel each target pixel covers,
// weighted by how much of it is covered. A plain sample of one pixel per target would drop the
// one-pixel lines (a caret, a focus ring, a table's rules) a model reads a screen by. A target at
// least as large as the source is returned as it is: the daemon never scales up.
func Downscale(src *image.RGBA, w, h int) *image.RGBA {
	sb := src.Bounds()
	sw, sh := sb.Dx(), sb.Dy()
	if w <= 0 || h <= 0 || (w >= sw && h >= sh) {
		return src
	}
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	fx, fy := float64(sw)/float64(w), float64(sh)/float64(h)
	// The weights of each target column over the source columns, computed once.
	type span struct {
		first int
		w     []float64
	}
	spans := func(n int, f float64, limit int) []span {
		out := make([]span, n)
		for i := range out {
			a, b := float64(i)*f, float64(i+1)*f
			first := int(a)
			var ws []float64
			for s := first; float64(s) < b && s < limit; s++ {
				lo, hi := max(a, float64(s)), min(b, float64(s+1))
				if hi > lo {
					ws = append(ws, hi-lo)
				}
			}
			out[i] = span{first: first, w: ws}
		}
		return out
	}
	cols, rows := spans(w, fx, sw), spans(h, fy, sh)
	area := fx * fy
	for y, row := range rows {
		for x, col := range cols {
			var r, g, b, a float64
			for j, wy := range row.w {
				off := src.PixOffset(sb.Min.X+col.first, sb.Min.Y+row.first+j)
				for i, wx := range col.w {
					p := src.Pix[off+4*i : off+4*i+4 : off+4*i+4]
					k := wx * wy
					r += float64(p[0]) * k
					g += float64(p[1]) * k
					b += float64(p[2]) * k
					a += float64(p[3]) * k
				}
			}
			o := dst.PixOffset(x, y)
			dst.Pix[o] = uint8(r/area + 0.5)
			dst.Pix[o+1] = uint8(g/area + 0.5)
			dst.Pix[o+2] = uint8(b/area + 0.5)
			dst.Pix[o+3] = uint8(a/area + 0.5)
		}
	}
	return dst
}

// Encode writes img in format; quality is JPEG's, 1–100.
func Encode(img image.Image, format string, quality int) ([]byte, error) {
	var buf bytes.Buffer
	switch format {
	case PNG:
		enc := png.Encoder{CompressionLevel: png.BestSpeed}
		if err := enc.Encode(&buf, img); err != nil {
			return nil, err
		}
	case JPEG:
		if quality < 1 || quality > 100 {
			return nil, fmt.Errorf("quality %d is not between 1 and 100", quality)
		}
		if err := jpeg.Encode(&buf, opaque(img), &jpeg.Options{Quality: quality}); err != nil {
			return nil, err
		}
	default:
		return nil, fmt.Errorf("format %q is neither png nor jpeg", format)
	}
	return buf.Bytes(), nil
}

// opaque drops alpha before JPEG: some captures leave it at zero, and a JPEG encoder handed
// premultiplied zero alpha writes black.
func opaque(img image.Image) image.Image {
	rgba, ok := img.(*image.RGBA)
	if !ok {
		return img
	}
	out := image.NewRGBA(rgba.Bounds())
	copy(out.Pix, rgba.Pix)
	for i := 3; i < len(out.Pix); i += 4 {
		out.Pix[i] = 255
	}
	return out
}
