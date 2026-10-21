package win

import (
	"slices"
	"testing"
)

func TestDisplayNames(t *testing.T) {
	devices := []string{`\\.\DISPLAY1`, `\\.\DISPLAY2`, `\\.\DISPLAY3`, `\\.\DISPLAY12`, `odd`}
	monitors := map[string]string{
		`\\.\DISPLAY1`: "DELL U2720Q",
		`\\.\DISPLAY2`: "DELL U2720Q",
		`\\.\DISPLAY3`: "  ",
	}
	got := displayNames(devices, monitors)
	want := []string{"DELL U2720Q (1)", "DELL U2720Q (2)", "Display 3", "Display 12", "odd"}
	if !slices.Equal(got, want) {
		t.Errorf("got %q, want %q", got, want)
	}
	if got := displayNames([]string{`\\.\DISPLAY2`}, map[string]string{`\\.\DISPLAY2`: "LG HDR 4K"}); got[0] != "LG HDR 4K" {
		t.Errorf("a single monitor keeps its bare name, got %q", got[0])
	}
}
