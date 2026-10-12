package view

import (
	"context"
	"fmt"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/wire"
)

var mouseTypes = map[string]string{"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}

var buttons = map[string]bool{"left": true, "middle": true, "right": true, "none": true, "": true}

// buttonMask is CDP's "buttons" for a button held down.
var buttonMask = map[string]int{"left": 1, "right": 2, "middle": 4}

// MaxInputText bounds one text input, as the contract does.
const MaxInputText = 1000

// dispatch turns one INPUT into the CDP input it stands for. Coordinates are CSS pixels of the
// viewport, as the client computed them from the frame's metadata.
func (h *Hub) dispatch(ctx context.Context, t *browser.Tab, in wire.Input) error {
	if in.Mods < 0 || in.Mods > 15 {
		return fmt.Errorf("mods must be 0-15")
	}
	switch in.T {
	case "mouse":
		typ, ok := mouseTypes[in.Type]
		if !ok || !buttons[in.Button] {
			return fmt.Errorf("mouse input needs type down, up or move and a known button")
		}
		button := in.Button
		if button == "" {
			button = "none"
		}
		p := map[string]any{"type": typ, "x": in.X, "y": in.Y, "button": button, "modifiers": in.Mods}
		if typ != "mouseMoved" {
			p["clickCount"] = max(1, in.Clicks)
		}
		if typ == "mousePressed" {
			p["buttons"] = buttonMask[button]
		}
		h.human(t, "mouse")
		t.SetPointer(in.X, in.Y)
		return t.Input(ctx, "Input.dispatchMouseEvent", p)
	case "wheel":
		h.human(t, "wheel")
		t.SetPointer(in.X, in.Y)
		return t.Input(ctx, "Input.dispatchMouseEvent", map[string]any{"type": "mouseWheel", "x": in.X, "y": in.Y,
			"deltaX": in.DX, "deltaY": in.DY, "modifiers": in.Mods})
	case "key":
		var typ string
		switch in.Type {
		case "down":
			// A key with text is keyDown, which also produces the character; without, rawKeyDown.
			typ = "rawKeyDown"
			if in.Text != "" {
				typ = "keyDown"
			}
		case "up":
			typ = "keyUp"
		default:
			return fmt.Errorf("key input needs type down or up")
		}
		p := map[string]any{"type": typ, "key": in.Key, "code": in.Code, "windowsVirtualKeyCode": in.KeyCode,
			"nativeVirtualKeyCode": in.KeyCode, "modifiers": in.Mods}
		if in.Text != "" && typ == "keyDown" {
			p["text"] = in.Text
			p["unmodifiedText"] = in.Text
		}
		h.human(t, "key")
		return t.Input(ctx, "Input.dispatchKeyEvent", p)
	case "text":
		if in.Text == "" || len([]rune(in.Text)) > MaxInputText {
			return fmt.Errorf("text input is 1-%d characters", MaxInputText)
		}
		h.human(t, "text")
		return t.Input(ctx, "Input.insertText", map[string]any{"text": in.Text})
	case "touch":
		types := map[string]string{"start": "touchStart", "move": "touchMove", "end": "touchEnd", "cancel": "touchCancel"}
		typ, ok := types[in.Type]
		if !ok || len(in.Points) > 10 {
			return fmt.Errorf("touch input needs type start, move, end or cancel and at most 10 points")
		}
		points := make([]map[string]any, 0, len(in.Points))
		for _, pt := range in.Points {
			points = append(points, map[string]any{"x": pt.X, "y": pt.Y, "id": pt.ID})
		}
		h.human(t, "touch")
		return t.Input(ctx, "Input.dispatchTouchEvent", map[string]any{"type": typ, "touchPoints": points, "modifiers": in.Mods})
	case "nav":
		h.human(t, "nav")
		// A person's navigation is not waited for: its progress arrives as tab updates, and the
		// input queue must not stall behind a slow page.
		go func() {
			nctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()
			var err error
			switch in.Action {
			case "url":
				if err = browser.CheckURL(in.URL, browser.ActorOperator); err == nil {
					_, err = h.m.Navigate(nctx, t, in.URL, 60*time.Second)
				}
			case "back":
				_, err = h.m.History(nctx, t, -1, 60*time.Second)
			case "forward":
				_, err = h.m.History(nctx, t, 1, 60*time.Second)
			case "reload":
				_, err = h.m.History(nctx, t, 0, 60*time.Second)
			default:
				err = fmt.Errorf("nav action must be url, back, forward or reload")
			}
			if err != nil {
				h.log.Info("a person's navigation failed", "tab", t.ID, "error", err.Error())
			}
		}()
		return nil
	}
	return fmt.Errorf("unknown input %q", in.T)
}

func (h *Hub) human(t *browser.Tab, kind string) {
	if h.HumanInput != nil {
		h.HumanInput(t, kind)
	}
}
