package page

import (
	"context"
	"encoding/json"
	"net"
	"net/url"
	"strings"
	"time"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The operator's private sign-in: the host hands over a username, a password and a one-time code the
// operator typed into a form of the app, and the daemon types them into the sign-in form of the
// agent's page and sends it. The model asked for the sign-in and never sees the values; neither does
// anything this file writes. A value goes from the request into the page as key presses and nowhere
// else: not into the page script's arguments (the script only finds the fields), not into an event,
// the log, the recording or an error. What comes back says which fields were filled and where the page
// ended up.
//
// Each value is typed only while the tab is on the site the operator was asked about. A page that
// moves elsewhere between the steps of a two-step sign-in (a redirect to a look-alike) gets nothing
// more; the result says where it went.

// LoginParams are page.login's.
type LoginParams struct {
	TabID     string         `json:"tab_id"`
	Site      string         `json:"site"`
	Username  string         `json:"username,omitempty"`
	Password  string         `json:"password,omitempty"`
	OTP       string         `json:"otp,omitempty"`
	Selectors LoginSelectors `json:"selectors,omitempty"`
	// Probe only looks for the form, typing nothing: the host asks before it troubles the operator,
	// so an agent on a page without a sign-in form is told so at once.
	Probe  bool            `json:"probe,omitempty"`
	Origin *browser.Origin `json:"origin,omitempty"`
}

// LoginSelectors are CSS selectors the agent gave for a form the guess gets wrong.
type LoginSelectors struct {
	Username string `json:"username,omitempty"`
	Password string `json:"password,omitempty"`
	OTP      string `json:"otp,omitempty"`
	Submit   string `json:"submit,omitempty"`
}

// LoginResult is page.login's reply. It carries no value the request did.
type LoginResult struct {
	URL   string `json:"url"`
	Title string `json:"title"`
	// SignedIn is yes, no or unknown: a guess from the page after the last submit (see judge).
	SignedIn string          `json:"signed_in"`
	Filled   []string        `json:"filled"`
	Found    map[string]bool `json:"found"`
	// Waiting is "code" when the page now asks for a one-time code the request did not carry.
	Waiting string `json:"waiting,omitempty"`
	// Stopped says why the sign-in stopped before every value was typed.
	Stopped string `json:"stopped,omitempty"`
}

// loginRefs are the fields loginFields found, as refs, "" where it found none.
type loginRefs struct {
	Username string `json:"username"`
	Password string `json:"password"`
	OTP      string `json:"otp"`
	Submit   string `json:"submit"`
}

func (f loginRefs) any() bool { return f.Username != "" || f.Password != "" || f.OTP != "" }

func (f loginRefs) found() map[string]bool {
	return map[string]bool{"username": f.Username != "", "password": f.Password != "", "otp": f.OTP != "", "submit": f.Submit != ""}
}

const (
	// loginStepWait is how long the next step of a sign-in (the password page after the username, the
	// code after the password) is waited for.
	loginStepWait = 10 * time.Second
	// loginOutcomeWait is how long the page after the last submit is watched for the form to go.
	loginOutcomeWait = 5 * time.Second
	loginPoll        = 250 * time.Millisecond
	maxLoginSteps    = 3
	maxSelector      = 300
)

// onSite says the address is on the site or under it: "example.com" holds www.example.com and
// accounts.example.com, never example.com.evil.test.
func onSite(address, site string) bool {
	u, err := url.Parse(address)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	site = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(site)), ".")
	if net.ParseIP(host) != nil {
		// An address has no parent: 127.0.0.1 is not "under" 0.0.1.
		return host == site
	}
	return site != "" && (host == site || strings.HasSuffix(host, "."+site))
}

func hostOf(address string) string {
	u, err := url.Parse(address)
	if err != nil || u.Hostname() == "" {
		return "a page with no address"
	}
	return u.Hostname()
}

func (lp LoginParams) check() error {
	if strings.Trim(strings.TrimSpace(lp.Site), ".") == "" {
		return wire.Errorf(wire.CodeInvalidParams, "site is required: the host the values may be typed on")
	}
	for _, s := range []string{lp.Selectors.Username, lp.Selectors.Password, lp.Selectors.OTP, lp.Selectors.Submit} {
		if len(s) > maxSelector {
			return wire.Errorf(wire.CodeInvalidParams, "a selector is at most %d characters", maxSelector)
		}
	}
	if lp.Probe {
		return nil
	}
	if lp.Username == "" && lp.Password == "" && lp.OTP == "" {
		return wire.Errorf(wire.CodeInvalidParams, "nothing to type: give a username, a password or a code")
	}
	// The lengths only, never the values, in what is said back.
	for _, v := range []string{lp.Username, lp.Password, lp.OTP} {
		if len(v) > MaxTypeText {
			return wire.Errorf(wire.CodeInvalidParams, "a value is at most %d characters", MaxTypeText)
		}
	}
	return nil
}

func (p *Model) loginFields(ctx context.Context, t *browser.Tab, sel LoginSelectors) (loginRefs, error) {
	var f loginRefs
	raw, err := p.call(ctx, t, "loginFields", sel)
	if err != nil {
		return f, err
	}
	var se scriptError
	if json.Unmarshal(raw, &se) == nil && se.Error != "" {
		return f, wire.Errorf(wire.CodeInvalidParams, "the selector for %s", se.Message)
	}
	_ = json.Unmarshal(raw, &f)
	return f, nil
}

// laterFields is loginFields on a page a submit led to, where the agent's selectors were written for
// the page before and may match nothing: there the guess stands in, rather than the sign-in failing on
// a page that has moved on.
func (p *Model) laterFields(ctx context.Context, t *browser.Tab, sel LoginSelectors) (loginRefs, error) {
	f, err := p.loginFields(ctx, t, sel)
	if err != nil && sel != (LoginSelectors{}) {
		return p.loginFields(ctx, t, LoginSelectors{})
	}
	return f, err
}

// Login fills and sends the sign-in form of the tab, in up to three steps: what the page shows of
// username, password and code is typed and sent, and a value whose field is not there yet is waited
// for on the page that follows.
func (p *Model) Login(ctx context.Context, t *browser.Tab, lp LoginParams) (*LoginResult, error) {
	if err := lp.check(); err != nil {
		return nil, err
	}
	res := &LoginResult{Filled: []string{}}
	fields, err := p.loginFields(ctx, t, lp.Selectors)
	if err != nil {
		return nil, err
	}
	res.Found = fields.found()
	if lp.Probe {
		res.URL, res.Title, res.SignedIn = t.URL(), t.Title(), "unknown"
		return res, nil
	}
	if !onSite(t.URL(), lp.Site) {
		return nil, wire.Errorf(wire.CodeForbidden, "the page is on %s, not %s; nothing was typed", hostOf(t.URL()), lp.Site)
	}
	if !fields.any() {
		return nil, wire.Errorf(wire.CodeNotFound, "no sign-in form on this page; nothing was typed")
	}
	start := t.URL()
	g := p.gesture(t)
	left := map[string]string{"username": lp.Username, "password": lp.Password, "otp": lp.OTP}
	for step := 0; step < maxLoginSteps; step++ {
		typed := false
		for _, name := range []string{"username", "password", "otp"} {
			ref := map[string]string{"username": fields.Username, "password": fields.Password, "otp": fields.OTP}[name]
			if left[name] == "" || ref == "" {
				continue
			}
			if !onSite(t.URL(), lp.Site) {
				res.Stopped = "the page moved to " + hostOf(t.URL()) + ", outside " + lp.Site
				break
			}
			if err := p.fillLogin(ctx, t, g, ref, left[name]); err != nil {
				return nil, err
			}
			left[name] = ""
			res.Filled = append(res.Filled, name)
			typed = true
		}
		if res.Stopped != "" || !typed {
			break
		}
		loaderBefore := t.Loader()
		if err := p.submitLogin(ctx, t, g, fields.Submit); err != nil {
			return nil, err
		}
		p.settle(ctx, t, loaderBefore)
		if left["username"] == "" && left["password"] == "" && left["otp"] == "" {
			break
		}
		next, moved := p.nextLoginStep(ctx, t, lp, left)
		if moved {
			res.Stopped = "the page moved to " + hostOf(t.URL()) + ", outside " + lp.Site
			break
		}
		fields = next
	}
	p.judge(ctx, t, lp, start, res)
	p.log.Info("login", "group", t.Group.ID, "tab", t.ID, "filled", strings.Join(res.Filled, ","), "signed_in", res.SignedIn)
	return res, nil
}

// nextLoginStep waits for the field of a value still to type to appear on the page the submit led
// to: the password after a username page, the code after a password. moved is true when the page
// left the site meanwhile.
func (p *Model) nextLoginStep(ctx context.Context, t *browser.Tab, lp LoginParams, left map[string]string) (loginRefs, bool) {
	deadline := time.Now().Add(loginStepWait)
	var f loginRefs
	for {
		if !onSite(t.URL(), lp.Site) {
			return f, true
		}
		if t.Dialog() == nil {
			if got, err := p.laterFields(ctx, t, lp.Selectors); err == nil {
				f = got
				if (left["password"] != "" && f.Password != "") || (left["otp"] != "" && f.OTP != "" && f.Password == "") {
					return f, false
				}
			}
		}
		if time.Now().After(deadline) || sleep(ctx, loginPoll) != nil {
			return f, false
		}
	}
}

// judge guesses whether the sign-in took, from the page after the last submit. A password field still
// asked for is a no; a code asked for that was not given is a wait; a page with no sign-in field left
// and another address is a yes; anything else is unknown, which the agent checks on the page itself.
func (p *Model) judge(ctx context.Context, t *browser.Tab, lp LoginParams, start string, res *LoginResult) {
	deadline := time.Now().Add(loginOutcomeWait)
	var f loginRefs
	for {
		if t.Dialog() != nil {
			break
		}
		got, err := p.laterFields(ctx, t, lp.Selectors)
		if err == nil {
			f = got
			// Without a password typed there is nothing to watch go: a page that now shows its
			// password field after the address is the answer as it stands.
			if f.Password == "" || t.URL() != start || lp.Password == "" {
				break
			}
		}
		if time.Now().After(deadline) || sleep(ctx, loginPoll) != nil {
			break
		}
	}
	res.URL, res.Title = t.URL(), t.Title()
	res.Found = f.found()
	switch {
	case res.Stopped != "":
		res.SignedIn = "unknown"
	case f.Password != "" && lp.Password != "":
		res.SignedIn = "no"
	case f.OTP != "" && lp.OTP == "":
		res.SignedIn, res.Waiting = "unknown", "code"
	case f.OTP != "":
		res.SignedIn = "no"
	case !f.any() && t.URL() != start:
		res.SignedIn = "yes"
	default:
		res.SignedIn = "unknown"
	}
}

// fillLogin types one value into one field: brought into view, clicked where nothing covers it (as a
// person's typing begins), its content selected and replaced. The field is then marked secret, so the
// outline shows [secret] where the username is rather than reading it back to the model.
func (p *Model) fillLogin(ctx context.Context, t *browser.Tab, g *gesture, ref, value string) error {
	pr, err := p.measureIn(ctx, t, top, ref, "prepare")
	if err != nil {
		return err
	}
	if pr.Element.Disabled {
		return wire.Errorf(wire.CodeForbidden, "the %s field is disabled", ref)
	}
	pt := aim(pr.Box, "login-"+ref)
	if at, err := p.covered(ctx, t, top, ref, pt); err == nil {
		if err := p.clickIn(ctx, t, top, ref, at, "left", 1, g); err != nil {
			return err
		}
	}
	if raw, err := p.call(ctx, t, "selectAll", ref); err != nil {
		return err
	} else if err := checkScript(raw); err != nil {
		return err
	}
	if err := g.typeText(ctx, value); err != nil {
		return err
	}
	raw, err := p.call(ctx, t, "markSecret", ref)
	if err != nil {
		return err
	}
	return checkScript(raw)
}

// submitLogin sends the form: its button where one was found and nothing covers it, else Enter in the
// field typed last, which sends a form as a person's Enter does.
func (p *Model) submitLogin(ctx context.Context, t *browser.Tab, g *gesture, submit string) error {
	if submit != "" {
		if pr, err := p.measureIn(ctx, t, top, submit, "prepare"); err == nil && !pr.Element.Disabled && pr.Box.W >= 1 && pr.Box.H >= 1 {
			if at, err := p.covered(ctx, t, top, submit, aim(pr.Box, "login-"+submit)); err == nil {
				return p.clickIn(ctx, t, top, submit, at, "left", 1, g)
			}
		}
	}
	return p.press(ctx, t, chord{key: named["Enter"]}, g)
}
