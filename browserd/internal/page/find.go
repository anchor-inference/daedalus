package page

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The bounds of page.find.
const (
	DefaultFindMatches = 20
	MaxFindMatches     = 100
	MaxFindQuery       = 500
	// findTimeout ends a search whose pattern backtracks without end: the page's own thread runs
	// the script and would hang with it.
	findTimeout = 3 * time.Second
)

// Control is an element a match is in or beside.
type Control struct {
	Ref  string `json:"ref"`
	Role string `json:"role"`
	Name string `json:"name"`
}

// Match is one place the text was found: the words around it, and the control it is in or the
// controls beside it.
type Match struct {
	Text string    `json:"text"`
	In   *Control  `json:"in,omitempty"`
	Near []Control `json:"near,omitempty"`
}

// Found is page.find's result: the matches, how many there were in all, and the same as lines to
// read.
type Found struct {
	URL     string  `json:"url"`
	Matches []Match `json:"matches"`
	Total   int     `json:"total"`
	Text    string  `json:"text"`
}

// Find searches the page's visible text, the frames of other sites' too, for a phrase or a regular
// expression, as a person's Ctrl+F: what the outline is too long to show whole, found without
// reading it all. Secret fields and hidden text are not searched.
func (p *Model) Find(ctx context.Context, t *browser.Tab, query string, regex, caseSensitive bool, max int) (*Found, error) {
	if strings.TrimSpace(query) == "" || len(query) > MaxFindQuery {
		return nil, wire.Errorf(wire.CodeInvalidParams, "query must be 1-%d bytes", MaxFindQuery)
	}
	if max == 0 {
		max = DefaultFindMatches
	}
	if max < 1 || max > MaxFindMatches {
		return nil, wire.Errorf(wire.CodeInvalidParams, "max must be 1-%d", MaxFindMatches)
	}
	out := &Found{URL: t.URL(), Matches: []Match{}}
	cross, _ := p.crossDocs(ctx, t, top, maxCrossFrames)
	for i, d := range append([]*doc{top}, cross...) {
		r, err := p.eval(ctx, t, d, "find", false, findTimeout, query, regex, caseSensitive, max-len(out.Matches))
		if err != nil {
			if i == 0 {
				return nil, err
			}
			continue
		}
		if err := checkScript(r.Result.Value); err != nil {
			return nil, err
		}
		var f struct {
			Matches []Match `json:"matches"`
			Total   int     `json:"total"`
		}
		if err := json.Unmarshal(r.Result.Value, &f); err != nil {
			return nil, err
		}
		out.Matches = append(out.Matches, f.Matches...)
		out.Total += f.Total
		if len(out.Matches) >= max {
			out.Matches = out.Matches[:max]
		}
	}
	out.Text = findText(query, out)
	return out, nil
}

func findText(query string, f *Found) string {
	var b strings.Builder
	switch {
	case f.Total == 0:
		fmt.Fprintf(&b, "- [no visible text matches %s]", quoteJSON(query))
		return b.String()
	case f.Total > len(f.Matches):
		fmt.Fprintf(&b, "- [%d matches for %s; the first %d]", f.Total, quoteJSON(query), len(f.Matches))
	default:
		fmt.Fprintf(&b, "- [%d %s for %s]", f.Total, plural(f.Total, "match", "matches"), quoteJSON(query))
	}
	for _, m := range f.Matches {
		b.WriteString("\n- text " + quoteJSON(m.Text))
		if m.In != nil {
			b.WriteString(" in " + control(*m.In))
		} else if len(m.Near) > 0 {
			parts := make([]string, len(m.Near))
			for i, c := range m.Near {
				parts[i] = control(c)
			}
			b.WriteString(" near " + strings.Join(parts, ", "))
		}
	}
	return b.String()
}

func control(c Control) string {
	s := c.Role
	if c.Name != "" {
		s += " " + quoteJSON(c.Name)
	}
	return s + " [ref=" + c.Ref + "]"
}

// quoteJSON quotes as the page script's JSON.stringify does: <, > and & left as they are.
func quoteJSON(s string) string {
	var b strings.Builder
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	_ = e.Encode(s)
	return strings.TrimSuffix(b.String(), "\n")
}

func plural(n int, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}
