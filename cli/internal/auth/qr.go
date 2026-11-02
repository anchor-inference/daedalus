package auth

import (
	"strings"

	"rsc.io/qr"
)

// QR draws text as a QR code in half-block characters, two modules per character cell vertically,
// with the quiet zone scanners need. With color it pins black on white, which scans whatever the
// terminal's theme; without it (NO_COLOR, a dumb terminal) it draws the light modules as blocks,
// which reads right on the dark background most terminals have.
func QR(text string, color bool) (string, error) {
	code, err := qr.Encode(text, qr.L)
	if err != nil {
		return "", err
	}
	const quiet = 2
	size := code.Size
	dark := func(x, y int) bool {
		if x < 0 || y < 0 || x >= size || y >= size {
			return false
		}
		return code.Black(x, y)
	}
	var b strings.Builder
	for y := -quiet; y < size+quiet; y += 2 {
		if color {
			b.WriteString("\x1b[30;47m")
		}
		for x := -quiet; x < size+quiet; x++ {
			top, bottom := dark(x, y), dark(x, y+1)
			if !color {
				top, bottom = !top, !bottom
			}
			switch {
			case top && bottom:
				b.WriteString("█")
			case top:
				b.WriteString("▀")
			case bottom:
				b.WriteString("▄")
			default:
				b.WriteString(" ")
			}
		}
		if color {
			b.WriteString("\x1b[0m")
		}
		b.WriteString("\n")
	}
	return b.String(), nil
}
