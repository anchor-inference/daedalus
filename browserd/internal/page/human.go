package page

import (
	"context"
	"math"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/human"
)

// maxTypedKeys is the longest text typed key by key. Past it the text is inserted in one piece as
// before: a pasted paragraph is what a person does with a long text too, and a thousand keys of
// three events each would cost seconds of round trips the budget does not see.
const maxTypedKeys = 200

// gesture is one action's human-like input: the planner, the tab, and what is left of the time the
// action may add. A plain gesture (input.human off) moves nothing and waits for nothing, and every
// method on it is then what the daemon did before.
type gesture struct {
	p      *Model
	t      *browser.Tab
	budget *human.Budget
}

func (p *Model) gesture(t *browser.Tab) *gesture {
	g := &gesture{p: p, t: t}
	if !p.inputCfg.Plain {
		g.budget = human.NewBudget(p.inputCfg.HumanBudget())
	}
	return g
}

func (g *gesture) on() bool { return g.budget != nil }

// wait sleeps up to d of what is left of the budget.
func (g *gesture) wait(ctx context.Context, lo, hi time.Duration) error {
	if !g.on() {
		return nil
	}
	return sleep(ctx, g.budget.Take(g.p.planner.Between(lo, hi)))
}

func sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	select {
	case <-time.After(d):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// from is where the pointer is: where the last mouse event left it, or, on a page the mouse has not
// been on, somewhere in the middle of the viewport, where it is moved first as a hand coming to
// rest there would put it.
func (g *gesture) from(ctx context.Context) (point, error) {
	if x, y, ok := g.t.Pointer(); ok {
		return point{X: x, Y: y}, nil
	}
	vp := g.t.Group.ViewportNow()
	at := point{X: float64(vp.W) * (0.3 + 0.4*g.p.planner.Float()),
		Y: float64(vp.H) * (0.3 + 0.4*g.p.planner.Float())}
	return at, g.p.mouse(ctx, g.t, "mouseMoved", at, "", 0, 0)
}

// travel moves the pointer toward to along a planned path, every step but the last: the caller
// sends that one, which is the move the press follows (and, into a frame of another site, the one
// that is checked for having arrived). It takes at most half of what is left of the budget, so the
// press and its pauses keep their share. buttons is the mask of buttons held, for a drag.
func (g *gesture) travel(ctx context.Context, to point, buttons int) error {
	if !g.on() {
		return nil
	}
	at, err := g.from(ctx)
	if err != nil {
		return err
	}
	dist := math.Hypot(to.X-at.X, to.Y-at.Y)
	d := g.budget.Take(min(human.TravelTime(dist), g.budget.Left()/2))
	steps := g.p.planner.Path(human.Point{X: at.X, Y: at.Y}, human.Point{X: to.X, Y: to.Y}, d)
	for _, s := range steps[:len(steps)-1] {
		if err := sleep(ctx, s.Wait); err != nil {
			return err
		}
		pt := point{X: s.At.X, Y: s.At.Y}
		if buttons != 0 {
			if err := g.t.Call(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseMoved", "x": pt.X, "y": pt.Y,
				"button": "left", "buttons": buttons}, nil); err != nil {
				return err
			}
			g.t.SetPointer(pt.X, pt.Y)
			continue
		}
		if err := g.p.mouse(ctx, g.t, "mouseMoved", pt, "", 0, 0); err != nil {
			return err
		}
	}
	if len(steps) > 0 {
		return sleep(ctx, steps[len(steps)-1].Wait)
	}
	return nil
}

// wheelIntoView scrolls an element of the page toward the middle of the viewport with the wheel, a
// notch at a time, where the pointer is, as a person scrolls to what they are about to click. It
// spends at most two fifths of the budget; what the wheel does not bring in, the instant scroll of
// prepare still does, so the action never fails for it. Elements in frames of other sites are left
// to prepare: the frame was brought into view on the way to them.
func (g *gesture) wheelIntoView(ctx context.Context, d *doc, ref string) {
	if !g.on() || d.parent != nil {
		return
	}
	pr, err := g.p.measureIn(ctx, g.t, d, ref, "measure")
	if err != nil || pr.Box.W < 1 || pr.Box.H < 1 {
		return
	}
	vh := pr.Viewport.H
	if pr.Box.Y >= 0 && pr.Box.Y+pr.Box.H <= vh {
		return
	}
	at, err := g.from(ctx)
	if err != nil {
		return
	}
	// The element's middle brought to somewhere between a third and two thirds of the way down.
	aimY := vh * (1.0/3 + g.p.planner.Float()/3)
	share := human.NewBudget(g.budget.Left() * 2 / 5)
	notches := human.WheelSteps(pr.Box.Y+pr.Box.H/2-aimY, max(2, int(share.Left()/(30*time.Millisecond))))
	for i, dy := range notches {
		if i > 0 {
			pause := share.Take(g.p.planner.Between(18*time.Millisecond, 40*time.Millisecond))
			g.budget.Take(pause)
			if sleep(ctx, pause) != nil {
				return
			}
		}
		if g.p.wheel(ctx, g.t, at, 0, dy) != nil {
			return
		}
	}
	// A wheel scrolls smoothly: the page arrives a moment after the last notch. Wait for it to come
	// to rest, within a third of what is left, so prepare measures it there and does not jump.
	settle := human.NewBudget(g.budget.Left() / 3)
	last := math.Inf(1)
	for settle.Left() > 0 {
		pause := settle.Take(16 * time.Millisecond)
		g.budget.Take(pause)
		if sleep(ctx, pause) != nil {
			return
		}
		now, err := g.p.measureIn(ctx, g.t, d, ref, "measure")
		if err != nil || now.Box.Y == last {
			return
		}
		last = now.Box.Y
	}
}

// typeText types text into the focused field, a key at a time with a typist's pauses, the pauses
// shrunk together to fit what is left of the budget. A text with a line break or a tab, or longer
// than maxTypedKeys, is inserted in one piece: Enter in a single-line field submits its form, and
// Tab leaves the field.
func (g *gesture) typeText(ctx context.Context, text string) error {
	n := utf8.RuneCountInString(text)
	if !g.on() || n > maxTypedKeys || strings.ContainsFunc(text, unicode.IsControl) {
		return g.p.input(ctx, g.t, "Input.insertText", map[string]any{"text": text})
	}
	delays := g.p.planner.KeyDelays(n, g.budget.Left())
	shift := false
	shiftKey := modifiers["Shift"].key
	i := 0
	for _, r := range text {
		if err := sleep(ctx, g.budget.Take(delays[i])); err != nil {
			return err
		}
		i++
		k := charKey(r, false)
		if r == ' ' {
			k = named["Space"]
		}
		if need := shifted(r); need != shift {
			typ := "rawKeyDown"
			if !need {
				typ = "keyUp"
			}
			if err := g.p.input(ctx, g.t, "Input.dispatchKeyEvent", map[string]any{"type": typ, "key": shiftKey.Key,
				"code": shiftKey.Code, "windowsVirtualKeyCode": shiftKey.KeyCode, "modifiers": boolBit(need, 8)}); err != nil {
				return err
			}
			shift = need
		}
		mods := boolBit(shift, 8)
		if err := g.p.input(ctx, g.t, "Input.dispatchKeyEvent", map[string]any{"type": "keyDown", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode, "text": k.Text, "unmodifiedText": k.Text, "modifiers": mods}); err != nil {
			return err
		}
		if err := g.p.input(ctx, g.t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": k.Key, "code": k.Code,
			"windowsVirtualKeyCode": k.KeyCode, "modifiers": mods}); err != nil {
			return err
		}
	}
	if shift {
		return g.p.input(ctx, g.t, "Input.dispatchKeyEvent", map[string]any{"type": "keyUp", "key": shiftKey.Key,
			"code": shiftKey.Code, "windowsVirtualKeyCode": shiftKey.KeyCode})
	}
	return nil
}

// shifted says whether a person holds Shift to type r on a US keyboard: capitals and the symbols
// above the digits and beside the letters.
func shifted(r rune) bool {
	return unicode.IsUpper(r) || strings.ContainsRune(`~!@#$%^&*()_+{}|:"<>?`, r)
}

func boolBit(b bool, bit int) int {
	if b {
		return bit
	}
	return 0
}
