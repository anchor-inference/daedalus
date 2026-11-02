package tui

import (
	"fmt"
	"strings"
	"time"
)

// verbs are what the spinner says the agent is doing.
//
// A whimsical gerund rather than "Working…" because the line sits on screen for minutes at a time
// and a fixed word stops being read; a changing one is a small sign of life that costs nothing. The
// list is our own, with a nod to the workshop the agent is named after.
var verbs = []string{
	"Pondering", "Tinkering", "Forging", "Whittling", "Drafting", "Sketching", "Chiselling",
	"Joinering", "Soldering", "Riveting", "Lathing", "Plotting", "Scheming", "Mulling",
	"Ruminating", "Cogitating", "Puzzling", "Untangling", "Unravelling", "Threading",
	"Labyrinthing", "Mazing", "Wing-waxing", "Feather-gluing", "Updrafting", "Gliding",
	"Soaring", "Charting", "Surveying", "Measuring", "Calibrating", "Tuning", "Polishing",
	"Burnishing", "Honing", "Tempering", "Annealing", "Smelting", "Casting", "Moulding",
	"Carving", "Engraving", "Etching", "Weaving", "Knitting", "Stitching", "Braiding",
	"Brewing", "Simmering", "Steeping", "Marinating", "Percolating", "Distilling",
	"Fermenting", "Kneading", "Proofing", "Noodling", "Doodling", "Musing", "Wondering",
	"Contemplating", "Deliberating", "Reckoning", "Tallying", "Divining", "Conjuring",
	"Inventing", "Devising", "Contriving", "Engineering", "Architecting", "Scaffolding",
	"Assembling", "Fettling", "Pottering", "Puttering", "Fiddling", "Tweaking",
	"Hatching", "Incubating", "Germinating", "Sprouting", "Blossoming", "Crystallising",
	"Coalescing", "Synthesising", "Harmonising", "Orchestrating", "Choreographing",
	"Spelunking", "Excavating", "Unearthing", "Prospecting", "Sleuthing", "Deducing",
	"Decoding", "Deciphering", "Translating", "Transmuting", "Alchemising",
}

// verbFor picks the verb of a turn. Seeded by the turn's start so it stays put for the whole turn:
// a verb that changed every tick would read as noise.
func verbFor(seed int64) string {
	if seed < 0 {
		seed = -seed
	}
	return verbs[seed%int64(len(verbs))]
}

// formatElapsed is the turn's age as Claude Code shows it: seconds, then minutes and seconds.
func formatElapsed(d time.Duration) string {
	if d < 0 {
		d = 0
	}
	s := int(d / time.Second)
	if s < 60 {
		return fmt.Sprintf("%ds", s)
	}
	if s < 3600 {
		return fmt.Sprintf("%dm %ds", s/60, s%60)
	}
	return fmt.Sprintf("%dh %dm", s/3600, (s%3600)/60)
}

// formatTokens is a token count at the precision a glance needs: 950, 1.2k, 34k, 1.3M.
func formatTokens(n int) string {
	switch {
	case n < 1000:
		return fmt.Sprintf("%d", n)
	case n < 10_000:
		return strings.TrimSuffix(fmt.Sprintf("%.1f", float64(n)/1000), ".0") + "k"
	case n < 1_000_000:
		return fmt.Sprintf("%dk", n/1000)
	default:
		return strings.TrimSuffix(fmt.Sprintf("%.1f", float64(n)/1_000_000), ".0") + "M"
	}
}

// shimmer renders text with a lighter band sweeping across it, the way Claude Code's spinner verb
// glints. The band is three cells wide and moves one cell per frame; on a plain terminal the text is
// returned bare.
func (m *Model) shimmer(text string, frame int) string {
	t := m.theme
	if t.Plain {
		return text
	}
	runes := []rune(text)
	period := len(runes) + 8
	at := frame % period
	base := t.Fg(t.Palette.Accent)
	bright := t.Fg(t.Palette.AccentShimmer)
	var b strings.Builder
	for i, r := range runes {
		if i >= at-1 && i <= at+1 {
			b.WriteString(bright.Render(string(r)))
		} else {
			b.WriteString(base.Render(string(r)))
		}
	}
	return b.String()
}
