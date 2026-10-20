package x11

import (
	"fmt"
	"image"
	"math/bits"
)

// pixelFormat is how the server lays out a ZPixmap image: bytes per pixel, byte order and where
// each colour sits, from the root visual's masks.
type pixelFormat struct {
	bytesPerPixel int
	lsbFirst      bool
	red, green    uint32
	blue          uint32
}

// shiftOf is where a mask's lowest bit is and how many bits it has.
func shiftOf(mask uint32) (shift, width int) {
	if mask == 0 {
		return 0, 0
	}
	shift = bits.TrailingZeros32(mask)
	width = bits.OnesCount32(mask)
	return
}

// channel scales a masked value to eight bits.
func channel(px, mask uint32) uint8 {
	shift, width := shiftOf(mask)
	if width == 0 {
		return 0
	}
	v := (px & mask) >> shift
	if width >= 8 {
		return uint8(v >> (width - 8))
	}
	// Spread a narrow channel (16-bit displays' five and six bits) over the full range.
	return uint8(v * 255 / (1<<width - 1))
}

// toRGBA converts w×h pixels of a ZPixmap reply into RGBA. Rows are padded to the format's scanline
// unit, so the stride is computed, not assumed.
func toRGBA(data []byte, w, h, scanlinePad int, f pixelFormat) (*image.RGBA, error) {
	if f.bytesPerPixel != 4 && f.bytesPerPixel != 2 && f.bytesPerPixel != 3 {
		return nil, fmt.Errorf("a %d-byte pixel format is not supported", f.bytesPerPixel)
	}
	padBits := max(8, scanlinePad)
	stride := ((w*f.bytesPerPixel*8 + padBits - 1) / padBits) * padBits / 8
	if len(data) < stride*(h-1)+w*f.bytesPerPixel {
		return nil, fmt.Errorf("the image has %d bytes, short of %d×%d", len(data), w, h)
	}
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		row := data[y*stride:]
		out := img.Pix[y*img.Stride:]
		for x := 0; x < w; x++ {
			p := row[x*f.bytesPerPixel:]
			var px uint32
			for i := 0; i < f.bytesPerPixel; i++ {
				if f.lsbFirst {
					px |= uint32(p[i]) << (8 * i)
				} else {
					px = px<<8 | uint32(p[i])
				}
			}
			o := out[x*4:]
			o[0], o[1], o[2], o[3] = channel(px, f.red), channel(px, f.green), channel(px, f.blue), 255
		}
	}
	return img, nil
}
