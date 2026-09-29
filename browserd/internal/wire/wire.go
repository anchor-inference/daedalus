// Package wire is the format of a live view: the frames a view channel carries between the daemon,
// the host's relay and the app. The host relays them unchanged, so this one codec is the format on
// both hops. The first byte is the type; integers are big-endian. The socket framing underneath is
// ptyd/proto/wire.
package wire

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
)

// The frame types. They are apart from the terminal frames' (0x01–0x13), so a frame that reaches
// the wrong kind of channel is refused rather than misread.
const (
	TypeFrame  byte = 0x21 // to the client: [u32 frame_no][u16 meta_len][meta JSON][JPEG]
	TypeEvent  byte = 0x22 // to the client: [JSON object with a string "type"]
	TypeAttach byte = 0x30 // to the daemon: [JSON]
	TypeAck    byte = 0x31 // to the daemon: [u32 frame_no]
	TypeView   byte = 0x32 // to the daemon: [JSON]
	TypeInput  byte = 0x33 // to the daemon: [JSON object with a string "t"]
)

// MaxInput bounds one INPUT frame. Input is keystrokes and pointer moves; text longer than a
// frame's worth arrives as several.
const MaxInput = 4 << 10

// MaxClientJSON bounds ATTACH and VIEW, which are a handful of numbers.
const MaxClientJSON = 4 << 10

// Meta describes one picture: its size in pixels, the viewport's in CSS pixels, and the page's
// scroll and zoom when it was captured. The field order is the wire's: the golden frames hold the
// encoding byte for byte.
type Meta struct {
	Tab       string  `json:"tab"`
	Tier      string  `json:"tier"`
	W         int     `json:"w"`
	H         int     `json:"h"`
	VW        float64 `json:"vw"`
	VH        float64 `json:"vh"`
	ScrollX   float64 `json:"scroll_x"`
	ScrollY   float64 `json:"scroll_y"`
	OffsetTop float64 `json:"offset_top"`
	PageScale float64 `json:"page_scale"`
	TS        int64   `json:"ts"` // capture time, milliseconds since the epoch
}

// Attach is the JSON of an ATTACH frame; View is a VIEW frame's, where every field is optional.
type Attach struct {
	Tier    string  `json:"tier"`
	Tab     string  `json:"tab,omitempty"`
	MaxW    int     `json:"max_w"`
	MaxH    int     `json:"max_h"`
	DPR     float64 `json:"dpr,omitempty"`
	Quality int     `json:"quality,omitempty"`
}

type View struct {
	Tier    string  `json:"tier,omitempty"`
	Tab     string  `json:"tab,omitempty"`
	MaxW    int     `json:"max_w,omitempty"`
	MaxH    int     `json:"max_h,omitempty"`
	DPR     float64 `json:"dpr,omitempty"`
	Quality int     `json:"quality,omitempty"`
	// Hidden says the client's page went out of view (true) or came back (false): a hidden client
	// is sent no frames, its mailbox keeping the newest for when it is back.
	Hidden *bool `json:"hidden,omitempty"`
}

// Input is an INPUT frame. Which fields mean something depends on T; coordinates are CSS pixels of
// the viewport, and Mods is CDP's bit set (1 Alt, 2 Ctrl, 4 Meta, 8 Shift).
type Input struct {
	T       string       `json:"t"`
	Type    string       `json:"type,omitempty"`
	X       float64      `json:"x,omitempty"`
	Y       float64      `json:"y,omitempty"`
	Button  string       `json:"button,omitempty"`
	Clicks  int          `json:"clicks,omitempty"`
	DX      float64      `json:"dx,omitempty"`
	DY      float64      `json:"dy,omitempty"`
	Key     string       `json:"key,omitempty"`
	Code    string       `json:"code,omitempty"`
	KeyCode int          `json:"key_code,omitempty"`
	Text    string       `json:"text,omitempty"`
	Mods    int          `json:"mods,omitempty"`
	Points  []TouchPoint `json:"points,omitempty"`
	Action  string       `json:"action,omitempty"`
	URL     string       `json:"url,omitempty"`
	// ID names a copy, so the client can match the "copied" event that answers it.
	ID string `json:"id,omitempty"`
}

type TouchPoint struct {
	X  float64 `json:"x"`
	Y  float64 `json:"y"`
	ID int     `json:"id"`
}

// Frame is one decoded view frame. Only the fields of its Type are meaningful.
type Frame struct {
	Type    byte
	FrameNo uint32 // FRAME, ACK
	Meta    Meta   // FRAME
	Image   []byte // FRAME
	JSON    []byte // EVENT, ATTACH, VIEW, INPUT: the object as it came
}

// EncodeFrame builds a FRAME. The meta is encoded compactly in its declared order.
func EncodeFrame(frameNo uint32, meta Meta, image []byte) ([]byte, error) {
	m, err := json.Marshal(meta)
	if err != nil {
		return nil, err
	}
	if len(m) > 0xffff {
		return nil, errors.New("frame meta longer than 65535 bytes")
	}
	b := make([]byte, 7, 7+len(m)+len(image))
	b[0] = TypeFrame
	binary.BigEndian.PutUint32(b[1:5], frameNo)
	binary.BigEndian.PutUint16(b[5:7], uint16(len(m)))
	b = append(b, m...)
	return append(b, image...), nil
}

// EncodeEvent builds an EVENT from any value that encodes to a JSON object with a string "type".
func EncodeEvent(v any) ([]byte, error) {
	j, err := marshalCompact(v)
	if err != nil {
		return nil, err
	}
	return append([]byte{TypeEvent}, j...), nil
}

// EncodeAck builds an ACK.
func EncodeAck(frameNo uint32) []byte {
	b := make([]byte, 5)
	b[0] = TypeAck
	binary.BigEndian.PutUint32(b[1:], frameNo)
	return b
}

// EncodeJSON builds an ATTACH, VIEW or INPUT frame.
func EncodeJSON(typ byte, v any) ([]byte, error) {
	j, err := marshalCompact(v)
	if err != nil {
		return nil, err
	}
	return append([]byte{typ}, j...), nil
}

// marshalCompact is json.Marshal without HTML escaping: the frames are not HTML, and the golden
// frames hold "<" and "&" as themselves, as every other codec writes them.
func marshalCompact(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buf.Bytes(), []byte("\n")), nil
}

// Decode parses one view frame from either direction. It checks the shape, not the meaning: an
// INPUT's fields are judged by whoever acts on them.
func Decode(b []byte) (Frame, error) {
	if len(b) == 0 {
		return Frame{}, errors.New("empty frame")
	}
	f := Frame{Type: b[0]}
	body := b[1:]
	switch f.Type {
	case TypeFrame:
		if len(body) < 6 {
			return f, errors.New("FRAME shorter than its header")
		}
		f.FrameNo = binary.BigEndian.Uint32(body[:4])
		if f.FrameNo == 0 {
			return f, errors.New("FRAME number 0")
		}
		n := int(binary.BigEndian.Uint16(body[4:6]))
		if 6+n > len(body) {
			return f, errors.New("FRAME meta runs past the frame")
		}
		if err := objectInto(body[6:6+n], &f.Meta); err != nil {
			return f, fmt.Errorf("FRAME meta: %w", err)
		}
		f.Image = body[6+n:]
		if len(f.Image) == 0 {
			return f, errors.New("FRAME without an image")
		}
	case TypeAck:
		if len(body) != 4 {
			return f, fmt.Errorf("ACK of %d bytes, want 4", len(body))
		}
		f.FrameNo = binary.BigEndian.Uint32(body)
	case TypeEvent:
		if err := typedObject(body, "type"); err != nil {
			return f, fmt.Errorf("EVENT: %w", err)
		}
		f.JSON = body
	case TypeAttach, TypeView:
		if len(body) > MaxClientJSON {
			return f, errors.New("ATTACH or VIEW longer than 4 KiB")
		}
		var m map[string]json.RawMessage
		if err := json.Unmarshal(body, &m); err != nil || m == nil {
			return f, errors.New("ATTACH or VIEW is not a JSON object")
		}
		f.JSON = body
	case TypeInput:
		if len(body) > MaxInput {
			return f, errors.New("INPUT longer than 4 KiB")
		}
		if err := typedObject(body, "t"); err != nil {
			return f, fmt.Errorf("INPUT: %w", err)
		}
		f.JSON = body
	default:
		return f, fmt.Errorf("unknown view frame type 0x%02x", f.Type)
	}
	return f, nil
}

// objectInto decodes a JSON object, refusing anything else (an array would decode into a struct as
// nothing at all).
func objectInto(b []byte, v any) error {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil || m == nil {
		return errors.New("not a JSON object")
	}
	return json.Unmarshal(b, v)
}

// typedObject checks that b is a JSON object whose field key is a non-empty string.
func typedObject(b []byte, key string) error {
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil || m == nil {
		return errors.New("not a JSON object")
	}
	var s string
	if err := json.Unmarshal(m[key], &s); err != nil || s == "" {
		return fmt.Errorf("no string %q", key)
	}
	return nil
}

// DecodeAttach and DecodeView read the JSON of those frames strictly: an unknown field is refused,
// so a client built against another version of this contract learns so at once.
func DecodeAttach(f Frame) (Attach, error) {
	var a Attach
	err := strict(f.JSON, &a)
	return a, err
}

func DecodeView(f Frame) (View, error) {
	var v View
	err := strict(f.JSON, &v)
	return v, err
}

func DecodeInput(f Frame) (Input, error) {
	var in Input
	err := strict(f.JSON, &in)
	return in, err
}

func strict(b []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}
