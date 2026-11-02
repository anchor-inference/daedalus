package ax

import (
	"encoding/json"
	"regexp"
	"strings"

	"github.com/ascorblack/daedalus/deskd/internal/geom"
)

// ToolRect is a rectangle as the tool writes it.
type ToolRect struct {
	X      float64 `json:"x"`
	Y      float64 `json:"y"`
	Width  float64 `json:"width"`
	Height float64 `json:"height"`
}

// Rect is the rectangle in the daemon's own shape.
func (r ToolRect) Rect() geom.Rect { return geom.Rect{X: r.X, Y: r.Y, W: r.Width, H: r.Height} }

// ToolNode is one element as the tool writes it. Only the fields the daemon passes on are read; the
// rest (hints, DOM classes, native ids) stay behind.
type ToolNode struct {
	RefID            string     `json:"ref_id,omitempty"`
	Role             string     `json:"role"`
	Name             *string    `json:"name,omitempty"`
	Value            *string    `json:"value,omitempty"`
	Description      *string    `json:"description,omitempty"`
	States           []string   `json:"states,omitempty"`
	AvailableActions []string   `json:"available_actions,omitempty"`
	Bounds           *ToolRect  `json:"bounds,omitempty"`
	ChildrenCount    *int       `json:"children_count,omitempty"`
	SubtreeTruncated bool       `json:"subtree_truncated,omitempty"`
	Children         []ToolNode `json:"children,omitempty"`
}

// Node is one element as the daemon hands it to the host.
type Node struct {
	Ref           string     `json:"ref,omitempty"`
	Role          string     `json:"role"`
	Name          *string    `json:"name,omitempty"`
	Value         *string    `json:"value,omitempty"`
	Description   *string    `json:"description,omitempty"`
	States        []string   `json:"states,omitempty"`
	Actions       []string   `json:"actions,omitempty"`
	Secure        bool       `json:"secure,omitempty"`
	Bounds        *geom.Rect `json:"bounds,omitempty"`
	ChildrenCount *int       `json:"children_count,omitempty"`
	Truncated     bool       `json:"truncated,omitempty"`
	Children      []Node     `json:"children,omitempty"`
}

// RefInfo is what the daemon remembers of a reference it handed out.
type RefInfo struct {
	Role   string
	Secure bool
}

// IsSecure reports a field whose value must never be shown: the tool's own "secure" state, or a role
// that names a password. The tool withholds such values already; the daemon checks again because a
// toolkit that reports a password box only by its role (GTK4's entries, per the tool's own notes)
// would otherwise depend on the tool getting every case right.
func IsSecure(role string, states []string) bool {
	for _, s := range states {
		if strings.EqualFold(s, "secure") {
			return true
		}
	}
	return strings.Contains(strings.ToLower(role), "password")
}

// Convert turns the tool's tree into the daemon's and collects every reference in it.
func Convert(n ToolNode, refs map[string]RefInfo) Node {
	secure := IsSecure(n.Role, n.States)
	out := Node{Ref: n.RefID, Role: n.Role, Name: n.Name, Value: n.Value, Description: n.Description, States: n.States,
		Actions: n.AvailableActions, Secure: secure, ChildrenCount: n.ChildrenCount, Truncated: n.SubtreeTruncated}
	if secure {
		out.Value = nil
	}
	if n.Bounds != nil {
		r := n.Bounds.Rect()
		out.Bounds = &r
	}
	if n.RefID != "" && refs != nil {
		refs[n.RefID] = RefInfo{Role: n.Role, Secure: secure}
	}
	for _, c := range n.Children {
		out.Children = append(out.Children, Convert(c, refs))
	}
	return out
}

// Match is one element ax.find found.
type Match struct {
	Ref    string     `json:"ref,omitempty"`
	Role   string     `json:"role"`
	Name   *string    `json:"name,omitempty"`
	Value  *string    `json:"value,omitempty"`
	States []string   `json:"states,omitempty"`
	Secure bool       `json:"secure,omitempty"`
	Bounds *geom.Rect `json:"bounds,omitempty"`
}

// ConvertMatch is ConvertNode for a find match.
func ConvertMatch(n ToolNode, refs map[string]RefInfo) Match {
	c := Convert(ToolNode{RefID: n.RefID, Role: n.Role, Name: n.Name, Value: n.Value, States: n.States, Bounds: n.Bounds}, refs)
	return Match{Ref: c.Ref, Role: c.Role, Name: c.Name, Value: c.Value, States: c.States, Secure: c.Secure, Bounds: c.Bounds}
}

// PostState is an element read back after an action.
type PostState struct {
	Role   string   `json:"role"`
	States []string `json:"states,omitempty"`
	Value  *string  `json:"value,omitempty"`
}

// ToolPostState is the tool's.
type ToolPostState struct {
	Role   string   `json:"role"`
	States []string `json:"states,omitempty"`
	Value  *string  `json:"value,omitempty"`
}

// Post converts a read-back, removing a secure field's value; secure is what the snapshot said of
// the element, in case the read-back itself lost the state.
func (p *ToolPostState) Post(secure bool) *PostState {
	if p == nil {
		return nil
	}
	out := &PostState{Role: p.Role, States: p.States, Value: p.Value}
	if secure || IsSecure(p.Role, p.States) {
		out.Value = nil
	}
	return out
}

// SnapshotData is the tool's snapshot answer.
type SnapshotData struct {
	Window struct {
		ID string `json:"id"`
	} `json:"window"`
	SnapshotID string   `json:"snapshot_id"`
	RefCount   int      `json:"ref_count"`
	Complete   bool     `json:"complete"`
	Tree       ToolNode `json:"tree"`
}

// FindData is the tool's find answer.
type FindData struct {
	SnapshotID   string     `json:"snapshot_id"`
	Matches      []ToolNode `json:"matches"`
	RolesPresent []string   `json:"roles_present,omitempty"`
}

// ActionData is the tool's answer to an element action.
type ActionData struct {
	Disposition Disposition    `json:"disposition"`
	PostState   *ToolPostState `json:"post_state,omitempty"`
	Surfaces    []struct {
		Kind string `json:"type"`
	} `json:"surfaces,omitempty"`
}

// ToolWindow is one window of list-windows.
type ToolWindow struct {
	ID     string    `json:"id"`
	PID    int       `json:"pid"`
	Bounds *ToolRect `json:"bounds,omitempty"`
}

// refPattern is a qualified reference: the snapshot id, then the element's number. Anything else is
// refused before the tool sees it, so a reference can never be read as a flag.
var refPattern = regexp.MustCompile(`^@([A-Za-z0-9]{1,64}):e([0-9]{1,7})$`)

// SnapshotOf returns the snapshot id of a well-formed reference.
func SnapshotOf(ref string) (string, bool) {
	m := refPattern.FindStringSubmatch(ref)
	if m == nil {
		return "", false
	}
	return m[1], true
}

// DecodeData reads a command's data; a shape it cannot read is the caller's "crashed".
func DecodeData(raw json.RawMessage, v any) error { return json.Unmarshal(raw, v) }
