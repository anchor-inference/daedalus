package win

import (
	"strings"
)

// displayNames names each display for a person: the monitor's own name when Windows knows it, else
// "Display N" after the number in the GDI device name, which is the number Windows' display settings
// show. Two identical monitors would read the same, so a repeated monitor name gets its number too.
// devices are the GDI device names ("\\.\DISPLAY1"), monitors maps them to monitor names.
func displayNames(devices []string, monitors map[string]string) []string {
	count := map[string]int{}
	for _, dev := range devices {
		if m := strings.TrimSpace(monitors[dev]); m != "" {
			count[m]++
		}
	}
	out := make([]string, len(devices))
	for i, dev := range devices {
		number := displayNumber(dev)
		m := strings.TrimSpace(monitors[dev])
		switch {
		case m != "" && count[m] > 1 && number != "":
			out[i] = m + " (" + number + ")"
		case m != "":
			out[i] = m
		case number != "":
			out[i] = "Display " + number
		default:
			out[i] = dev
		}
	}
	return out
}

// displayNumber is the trailing number of a GDI device name, "" when it has none.
func displayNumber(device string) string {
	i := len(device)
	for i > 0 && device[i-1] >= '0' && device[i-1] <= '9' {
		i--
	}
	if i == len(device) || !strings.HasSuffix(strings.ToUpper(device[:i]), "DISPLAY") {
		return ""
	}
	return device[i:]
}
