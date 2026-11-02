package tui

import (
	"context"
	"fmt"
	"image/color"
	"os"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/ascorblack/daedalus/cli/internal/tui/diff"
	"github.com/ascorblack/daedalus/cli/internal/tui/textedit"
)

// dialog is anything that takes the prompt's place to ask the person something: a permission
// request, a question, a picker, a panel. Claude Code draws its dialogs where the input was, so the
// transcript above stays in view and the eye does not have to travel to an overlay.
type dialog interface {
	view(m *Model) []string
	key(m *Model, msg tea.KeyPressMsg) tea.Cmd
	// cancel is Ctrl+C: what dismissing this dialog means.
	cancel(m *Model) tea.Cmd
	update(m *Model, msg dialogMsg) tea.Cmd
}

// dialogMsg carries an asynchronous result back to the dialog that asked for it.
type dialogMsg struct {
	kind string
	data any
	err  error
}

type paster interface{ paste(string) }

func userHome() (string, error) { return os.UserHomeDir() }

// frame draws a dialog's box: a rounded border in the dialog's accent, the full width.
func (m *Model) boxed(accent color.Color, body []string) []string {
	t := m.theme
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).Padding(0, 1).Width(m.width)
	if accent != nil && !t.Plain {
		box = box.BorderForeground(accent)
	}
	return strings.Split(box.Render(strings.Join(body, "\n")), "\n")
}

// option draws one numbered row of a choice list, with the pointer on the selected one.
func (m *Model) option(n int, label string, selected bool) string {
	t := m.theme
	if selected {
		return t.Fg(t.Palette.Permission).Render(t.Glyphs.Pointer+" "+fmt.Sprintf("%d. ", n)) + t.Fg(t.Palette.Permission).Render(label)
	}
	return "  " + t.Dim().Render(fmt.Sprintf("%d. ", n)) + t.Fg(t.Palette.Text).Render(label)
}

// permissionDialog is Claude Code's numbered permission prompt.
type permissionDialog struct {
	request  PermissionRequest
	cursor   int
	feedback *textedit.Editor
	typing   bool
}

func (m *Model) openPermission(request PermissionRequest) {
	m.dialog = &permissionDialog{request: request, feedback: textedit.New()}
	m.comp, m.help, m.search = nil, false, nil
}

func (d *permissionDialog) choices(m *Model) []string {
	agent := m.options.AgentName
	out := []string{"Yes"}
	if d.request.Similar != "" {
		out = append(out, "Yes, and don't ask again for "+d.request.Similar)
	}
	return append(out, "No, and tell "+agent+" what to do differently (esc)")
}

func (d *permissionDialog) title() string {
	display, _ := toolTitle(d.request.Tool, nil, "")
	switch {
	case d.request.Change != nil && d.request.Change.Before == "":
		return "Create file"
	case d.request.Change != nil:
		return "Edit file"
	case display == "Bash":
		return "Bash command"
	case d.request.Kind == "handoff":
		return "Your turn"
	case display == "":
		return "Permission"
	}
	return display
}

func (d *permissionDialog) view(m *Model) []string {
	t := m.theme
	accent := t.Palette.Permission
	if d.request.Elevated {
		accent = t.Palette.Warning
	}
	title := t.Fg(accent).Bold(true).Render(d.title())
	if d.request.Source == SourceLocal {
		title += t.Dim().Render("  · asked by the local guard")
	}
	body := []string{title, ""}
	inner := max(m.width-6, 10)
	if d.request.Change != nil {
		lines := diff.Lines(d.request.Change.Before, d.request.Change.After)
		body = append(body, t.Bold().Render(relPath(d.request.Change.Path, m.cwdForDisplay())))
		body = append(body, diff.Render(t, lines, inner, max(m.height/2, 8))...)
	} else {
		for _, line := range wrapPlain(d.request.Text, inner-2) {
			body = append(body, "  "+t.Fg(t.Palette.Text).Render(line))
		}
	}
	question := "Do you want to proceed?"
	if d.request.Change != nil {
		question = "Do you want to make this edit to " + baseName(d.request.Change.Path) + "?"
	}
	if d.request.Elevated {
		body = append(body, "", t.Fg(t.Palette.Warning).Render(t.Glyphs.Warning+" This touches the machine itself, not only the project."))
	}
	body = append(body, "", t.Fg(t.Palette.Text).Render(question))
	for i, c := range d.choices(m) {
		body = append(body, m.option(i+1, c, i == d.cursor))
	}
	if d.typing {
		rows, _, _ := d.feedback.Render(inner - 4)
		body = append(body, "")
		for i, r := range rows {
			lead := "  "
			if i == 0 {
				lead = t.Fg(t.Palette.Permission).Render(t.Glyphs.Prompt) + " "
			}
			if i == 0 && d.feedback.Empty() {
				r = t.Faint().Render("Tell " + m.options.AgentName + " what to do instead, then enter")
			}
			body = append(body, lead+r)
		}
	}
	return m.boxed(accent, body)
}

func baseName(p string) string {
	if i := strings.LastIndexAny(p, `/\`); i >= 0 {
		return p[i+1:]
	}
	return p
}

func (d *permissionDialog) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	n := len(d.choices(m))
	if d.typing {
		switch msg.String() {
		case "esc":
			d.typing = false
			return nil
		}
		if d.feedback.HandleKey(msg) == textedit.Submit {
			return d.decide(m, Deny, strings.TrimSpace(d.feedback.Expanded()))
		}
		return nil
	}
	switch k := msg.String(); k {
	case "up", "k", "shift+tab":
		d.cursor = (d.cursor + n - 1) % n
	case "down", "j", "tab":
		d.cursor = (d.cursor + 1) % n
	case "enter":
		return d.choose(m, d.cursor)
	case "esc":
		return d.decide(m, Deny, "")
	case "1", "2", "3":
		i := int(k[0] - '1')
		if i < n {
			return d.choose(m, i)
		}
	}
	return nil
}

func (d *permissionDialog) choose(m *Model, i int) tea.Cmd {
	n := len(d.choices(m))
	switch {
	case i == 0:
		return d.decide(m, Allow, "")
	case i == n-1:
		// "No, and tell it what to do": the prompt opens inside the dialog, and enter sends the
		// refusal with the text, so the agent hears why in the same turn.
		d.cursor = i
		d.typing = true
		return nil
	default:
		return d.decide(m, AllowSimilar, "")
	}
}

func (d *permissionDialog) decide(m *Model, kind DecisionKind, feedback string) tea.Cmd {
	m.dialog = nil
	id := d.request.ID
	decision := Decision{Kind: kind, Feedback: feedback}
	return m.call("permission", func(ctx context.Context) error { return m.backend.Decide(ctx, id, decision) })
}

func (d *permissionDialog) cancel(m *Model) tea.Cmd {
	return tea.Batch(d.decide(m, Deny, ""), m.interrupt())
}

func (d *permissionDialog) update(m *Model, msg dialogMsg) tea.Cmd { return nil }

func (d *permissionDialog) paste(s string) {
	if d.typing {
		d.feedback.Paste(s)
	}
}

// questionDialog is an AskUser call: one tab per question, numbered options, a typed answer where
// the question allows one, and a review row when there is more than one question.
type questionDialog struct {
	question Question
	tab      int
	cursor   []int
	picked   []map[int]bool
	custom   []*textedit.Editor
	typing   bool
}

func (m *Model) openQuestion(q Question) {
	d := &questionDialog{question: q}
	for range q.Items {
		d.cursor = append(d.cursor, 0)
		d.picked = append(d.picked, map[int]bool{})
		d.custom = append(d.custom, textedit.New())
	}
	m.dialog = d
	m.comp, m.help, m.search = nil, false, nil
}

func (d *questionDialog) reviewing() bool {
	return len(d.question.Items) > 1 && d.tab == len(d.question.Items)
}

func (d *questionDialog) rows(i int) int {
	item := d.question.Items[i]
	n := len(item.Options)
	if item.Custom {
		n++
	}
	return n
}

func (d *questionDialog) view(m *Model) []string {
	t := m.theme
	g := t.Glyphs
	var body []string
	if len(d.question.Items) > 1 {
		var tabs []string
		for i, item := range d.question.Items {
			label := item.Header
			if label == "" {
				label = fmt.Sprintf("Question %d", i+1)
			}
			mark := g.Box
			if d.answered(i) {
				mark = g.BoxChecked
			}
			style := t.Dim()
			if i == d.tab {
				style = t.Fg(t.Palette.Permission).Bold(true)
			}
			tabs = append(tabs, style.Render(mark+" "+label))
		}
		submit := t.Dim().Render(g.Check + " Submit")
		if d.reviewing() {
			submit = t.Fg(t.Palette.Permission).Bold(true).Render(g.Check + " Submit")
		}
		body = append(body, t.Dim().Render("← ")+strings.Join(tabs, "  ")+"  "+submit+t.Dim().Render(" →"), "")
	}
	if d.reviewing() {
		body = append(body, t.Bold().Render("Review your answers"), "")
		for i, item := range d.question.Items {
			body = append(body, t.Dim().Render(g.Separator+" "+item.Text))
			body = append(body, "   "+t.Fg(t.Palette.Text).Render(strings.Join(d.answer(i).labels(), ", ")))
		}
		body = append(body, "", t.Fg(t.Palette.Permission).Render(g.Pointer+" Submit answers")+t.Dim().Render("  (enter)"))
		return m.boxed(t.Palette.Permission, body)
	}
	item := d.question.Items[d.tab]
	body = append(body, t.Bold().Render(item.Text))
	if item.Multi {
		body = append(body, t.Dim().Render("Select all that apply · space to toggle · enter to continue"))
	}
	body = append(body, "")
	inner := max(m.width-10, 10)
	for i, opt := range item.Options {
		label := opt.Label
		if item.Multi {
			mark := "[ ] "
			if d.picked[d.tab][i] {
				mark = "[" + g.Check + "] "
			}
			label = mark + label
		}
		body = append(body, m.option(i+1, label, d.cursor[d.tab] == i))
		if opt.Description != "" {
			for _, line := range wrapPlain(opt.Description, inner) {
				body = append(body, "     "+t.Dim().Render(line))
			}
		}
	}
	if item.Custom {
		n := len(item.Options)
		label := "Type something"
		editor := d.custom[d.tab]
		selected := d.cursor[d.tab] == n
		if selected && d.typing || !editor.Empty() {
			rows, _, _ := editor.Render(inner)
			label = rows[0]
			if editor.Empty() {
				label = t.Faint().Render("type your answer, enter to confirm")
			}
		}
		body = append(body, m.option(n+1, label, selected))
	}
	body = append(body, "", t.Dim().Render("enter to select · ↑/↓ to navigate · esc to cancel"))
	return m.boxed(t.Palette.Permission, body)
}

type answerView Answer

func (a answerView) labels() []string {
	out := append([]string(nil), a.Selected...)
	if a.Custom != "" {
		out = append(out, a.Custom)
	}
	if len(out) == 0 {
		out = []string{"(no answer)"}
	}
	return out
}

func (d *questionDialog) answer(i int) answerView {
	item := d.question.Items[i]
	var a Answer
	if item.Multi {
		for j, opt := range item.Options {
			if d.picked[i][j] {
				a.Selected = append(a.Selected, opt.Label)
			}
		}
	} else if c := d.cursor[i]; d.picked[i][c] && c < len(item.Options) {
		a.Selected = []string{item.Options[c].Label}
	}
	if !d.custom[i].Empty() {
		a.Custom = strings.TrimSpace(d.custom[i].Expanded())
	}
	return answerView(a)
}

func (d *questionDialog) answered(i int) bool {
	a := d.answer(i)
	return len(a.Selected) > 0 || a.Custom != ""
}

func (d *questionDialog) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	k := msg.String()
	tabs := len(d.question.Items)
	if len(d.question.Items) > 1 {
		tabs++
	}
	if d.reviewing() {
		switch k {
		case "enter":
			return d.submit(m)
		case "left", "shift+tab":
			d.tab--
		case "esc":
			return d.cancel(m)
		}
		return nil
	}
	item := d.question.Items[d.tab]
	n := d.rows(d.tab)
	onCustom := item.Custom && d.cursor[d.tab] == len(item.Options)
	if d.typing && onCustom {
		switch k {
		case "esc":
			d.typing = false
			return nil
		case "up", "down":
			d.typing = false
		default:
			if d.custom[d.tab].HandleKey(msg) == textedit.Submit {
				d.typing = false
				return d.advance(m)
			}
			return nil
		}
	}
	switch k {
	case "up", "k":
		d.cursor[d.tab] = (d.cursor[d.tab] + n - 1) % n
	case "down", "j":
		d.cursor[d.tab] = (d.cursor[d.tab] + 1) % n
	case "left", "shift+tab":
		if d.tab > 0 {
			d.tab--
		}
	case "right", "tab":
		if d.tab < tabs-1 {
			d.tab++
		}
	case "space":
		if item.Multi && !onCustom {
			d.picked[d.tab][d.cursor[d.tab]] = !d.picked[d.tab][d.cursor[d.tab]]
		}
	case "enter":
		if onCustom {
			d.typing = true
			return nil
		}
		if item.Multi {
			if !d.answered(d.tab) {
				d.picked[d.tab][d.cursor[d.tab]] = true
			}
		} else {
			d.picked[d.tab] = map[int]bool{d.cursor[d.tab]: true}
		}
		return d.advance(m)
	case "esc":
		return d.cancel(m)
	default:
		if len(k) == 1 && k[0] >= '1' && k[0] <= '9' {
			i := int(k[0] - '1')
			if i < n {
				d.cursor[d.tab] = i
				if item.Custom && i == len(item.Options) {
					d.typing = true
					return nil
				}
				if item.Multi {
					d.picked[d.tab][i] = !d.picked[d.tab][i]
					return nil
				}
				d.picked[d.tab] = map[int]bool{i: true}
				return d.advance(m)
			}
		}
	}
	return nil
}

// advance moves to the next question, or submits after the last one when there is only one.
func (d *questionDialog) advance(m *Model) tea.Cmd {
	if len(d.question.Items) == 1 {
		return d.submit(m)
	}
	d.tab++
	return nil
}

func (d *questionDialog) submit(m *Model) tea.Cmd {
	answers := make([]Answer, len(d.question.Items))
	for i := range d.question.Items {
		answers[i] = Answer(d.answer(i))
	}
	m.dialog = nil
	id := d.question.ID
	return m.call("answer", func(ctx context.Context) error { return m.backend.Answer(ctx, id, answers) })
}

// cancel leaves the question unanswered and stops the turn, as Esc on Claude Code's question does:
// the agent is waiting on it and would otherwise wait forever.
func (d *questionDialog) cancel(m *Model) tea.Cmd {
	m.dialog = nil
	return m.interrupt()
}

func (d *questionDialog) update(m *Model, msg dialogMsg) tea.Cmd { return nil }

func (d *questionDialog) paste(s string) {
	if d.typing {
		d.custom[d.tab].Paste(s)
	}
}

// pickItem is one row of a picker.
type pickItem struct {
	label  string
	detail string
	value  any
	right  string
}

// picker is a filterable list: the session list, the models, the rewind points, the settings.
type picker struct {
	title    string
	subtitle string
	items    []pickItem
	cursor   int
	filter   string
	// filterable lets typed letters narrow the list; otherwise digits choose a row.
	filterable bool
	loading    bool
	err        string
	footer     string
	choose     func(m *Model, item pickItem) tea.Cmd
	// side handles left and right, for the model picker's effort.
	side func(m *Model, item *pickItem, delta int)
	// loaded receives the async result that fills the list.
	loaded func(m *Model, p *picker, data any)
}

func (p *picker) visible() []int {
	var out []int
	needle := strings.ToLower(p.filter)
	for i, item := range p.items {
		if needle == "" || strings.Contains(strings.ToLower(item.label+" "+item.detail), needle) {
			out = append(out, i)
		}
	}
	return out
}

func (p *picker) view(m *Model) []string {
	t := m.theme
	body := []string{t.Fg(t.Palette.Permission).Bold(true).Render(p.title)}
	if p.subtitle != "" {
		body = append(body, t.Dim().Render(p.subtitle))
	}
	if p.filterable {
		body = append(body, t.Dim().Render(t.Glyphs.Prompt+" ")+p.filter+t.Faint().Render("▏"))
	}
	body = append(body, "")
	switch {
	case p.loading:
		body = append(body, t.Dim().Render("Loading"+t.Glyphs.Ellipsis))
	case p.err != "":
		body = append(body, t.Fg(t.Palette.Error).Render(p.err))
	default:
		visible := p.visible()
		if len(visible) == 0 {
			body = append(body, t.Dim().Render("Nothing matches"))
		}
		height := max(min(m.height/2, 12), 3)
		start := 0
		if p.cursor >= height {
			start = p.cursor - height + 1
		}
		inner := max(m.width-8, 10)
		for row := start; row < len(visible) && row < start+height; row++ {
			item := p.items[visible[row]]
			label := item.label
			if item.detail != "" {
				label += "  " + t.Dim().Render(item.detail)
			}
			if item.right != "" {
				label = joinEnds(label, t.Dim().Render(item.right), inner)
			}
			label = ansi.Truncate(label, inner, t.Glyphs.Ellipsis)
			if row == p.cursor {
				body = append(body, t.Fg(t.Palette.Permission).Render(t.Glyphs.Pointer+" ")+t.Fg(t.Palette.Permission).Render(ansi.Strip(label)))
			} else {
				body = append(body, "  "+label)
			}
		}
	}
	footer := p.footer
	if footer == "" {
		footer = "enter to choose · esc to cancel"
	}
	body = append(body, "", t.Dim().Render(footer))
	return m.boxed(t.Palette.Permission, body)
}

func (p *picker) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	visible := p.visible()
	k := msg.String()
	switch k {
	case "esc":
		m.dialog = nil
		return nil
	case "up", "ctrl+p":
		if p.cursor > 0 {
			p.cursor--
		}
		return nil
	case "down", "ctrl+n":
		if p.cursor < len(visible)-1 {
			p.cursor++
		}
		return nil
	case "left", "right":
		if p.side != nil && p.cursor < len(visible) {
			delta := 1
			if k == "left" {
				delta = -1
			}
			p.side(m, &p.items[visible[p.cursor]], delta)
		}
		return nil
	case "enter":
		if p.cursor < len(visible) && p.choose != nil {
			return p.choose(m, p.items[visible[p.cursor]])
		}
		return nil
	case "backspace":
		if p.filterable && p.filter != "" {
			r := []rune(p.filter)
			p.filter = string(r[:len(r)-1])
			p.cursor = 0
		}
		return nil
	}
	if p.filterable && len(msg.Text) > 0 && msg.Mod == 0 {
		p.filter += msg.Text
		p.cursor = 0
		return nil
	}
	if !p.filterable && len(k) == 1 && k[0] >= '1' && k[0] <= '9' {
		i := int(k[0] - '1')
		if i < len(visible) && p.choose != nil {
			p.cursor = i
			return p.choose(m, p.items[visible[i]])
		}
	}
	return nil
}

func (p *picker) cancel(m *Model) tea.Cmd {
	m.dialog = nil
	return nil
}

func (p *picker) update(m *Model, msg dialogMsg) tea.Cmd {
	p.loading = false
	if msg.err != nil {
		p.err = msg.err.Error()
		return nil
	}
	if p.loaded != nil {
		p.loaded(m, p, msg.data)
	}
	return nil
}

func (p *picker) paste(s string) {
	if p.filterable {
		p.filter += strings.ReplaceAll(s, "\n", " ")
	}
}

// panel is a read-only box: help, status, usage. Esc or enter closes it.
type panel struct {
	title   string
	lines   []string
	loading bool
	err     string
	loaded  func(m *Model, p *panel, data any)
}

func (p *panel) view(m *Model) []string {
	t := m.theme
	body := []string{t.Fg(t.Palette.Permission).Bold(true).Render(p.title), ""}
	switch {
	case p.loading:
		body = append(body, t.Dim().Render("Loading"+t.Glyphs.Ellipsis))
	case p.err != "":
		body = append(body, t.Fg(t.Palette.Error).Render(p.err))
	default:
		body = append(body, p.lines...)
	}
	body = append(body, "", t.Dim().Render("esc to close"))
	return m.boxed(t.Palette.Permission, body)
}

func (p *panel) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	switch msg.String() {
	case "esc", "enter", "q", "space":
		m.dialog = nil
	}
	return nil
}

func (p *panel) cancel(m *Model) tea.Cmd {
	m.dialog = nil
	return nil
}

func (p *panel) update(m *Model, msg dialogMsg) tea.Cmd {
	p.loading = false
	if msg.err != nil {
		p.err = msg.err.Error()
		return nil
	}
	if p.loaded != nil {
		p.loaded(m, p, msg.data)
	}
	return nil
}

// loginDialog shows the pairing code and waits for the app to approve it.
type loginDialog struct {
	challenge LoginChallenge
	loading   bool
	err       string
	state     LoginState
	device    string
}

func (d *loginDialog) view(m *Model) []string {
	t := m.theme
	body := []string{t.Fg(t.Palette.Permission).Bold(true).Render("Pair this terminal"), ""}
	switch {
	case d.loading:
		body = append(body, t.Dim().Render("Asking the server for a code"+t.Glyphs.Ellipsis))
	case d.err != "":
		body = append(body, t.Fg(t.Palette.Error).Render(d.err))
	case d.state == LoginApproved:
		body = append(body, t.Fg(t.Palette.Success).Render(t.Glyphs.Check+" Paired as "+d.device))
	case d.state == LoginRefused:
		body = append(body, t.Fg(t.Palette.Error).Render(t.Glyphs.Cross+" The pairing was refused in the app."))
	case d.state == LoginExpired:
		body = append(body, t.Fg(t.Palette.Error).Render(t.Glyphs.Cross+" The code expired. Run /login again."))
	default:
		body = append(body,
			t.Fg(t.Palette.Text).Render("Approve this code in the app:"),
			"",
			"    "+t.Fg(t.Palette.Accent).Bold(true).Render(d.challenge.Code),
			"",
			t.Dim().Render("or open ")+t.Fg(t.Palette.Permission).Underline(true).Render(d.challenge.URL),
			"",
			t.Fg(t.Palette.Accent).Render(t.Glyphs.SpinnerFrame(m.frame))+t.Dim().Render(" Waiting for approval"+t.Glyphs.Ellipsis),
		)
		if !d.challenge.ExpiresAt.IsZero() {
			left := d.challenge.ExpiresAt.Sub(m.now())
			body = append(body, t.Faint().Render("The code expires in "+formatElapsed(left)+"."))
		}
	}
	body = append(body, "", t.Dim().Render("esc to close"))
	return m.boxed(t.Palette.Permission, body)
}

func (d *loginDialog) key(m *Model, msg tea.KeyPressMsg) tea.Cmd {
	switch msg.String() {
	case "esc", "enter", "q":
		m.dialog = nil
	}
	return nil
}

func (d *loginDialog) cancel(m *Model) tea.Cmd {
	m.dialog = nil
	return nil
}

func (d *loginDialog) update(m *Model, msg dialogMsg) tea.Cmd {
	d.loading = false
	if msg.err != nil {
		d.err = msg.err.Error()
		return nil
	}
	if c, ok := msg.data.(LoginChallenge); ok {
		d.challenge = c
	}
	return m.tick()
}
