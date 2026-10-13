package rpc_test

import (
	"encoding/json"
	"fmt"
	"html"
	"net/http"
	"strings"
	"testing"
)

// The operator's private sign-in (page.login) against sign-in forms of the local site: one step, two
// steps, a one-time code, a form only selectors find, a page that moved to another site, and the
// promise that no value comes back, reaches an event or the log.

const (
	loginUser     = "someone@example.com"
	loginPassword = "SECRET-login-pw-7"
	loginCode     = "SECRET-code-31"
)

func loginPages(mux *http.ServeMux) {
	write := func(w http.ResponseWriter, title, body string) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s</title></head><body style="font:16px sans-serif">%s</body></html>`, title, body)
	}
	oneStep := func(then, alert string) string {
		return alert + `<form method="post" action="/login/check"><input type="hidden" name="then" value="` + then + `">` +
			`<p><label>Email <input name="email" type="email"></label></p><p><label>Password <input name="password" type="password"></label></p>` +
			`<button type="submit">Sign in</button></form>`
	}
	codePage := func(alert string) string {
		return alert + `<form method="post" action="/login/code"><p><label>Code <input name="code" autocomplete="one-time-code" inputmode="numeric"></label></p>` +
			`<button type="submit">Verify</button></form>`
	}
	wrong := `<p role="alert">Wrong email or password</p>`
	mux.HandleFunc("/login/one", func(w http.ResponseWriter, r *http.Request) { write(w, "Sign in", oneStep("", "")) })
	mux.HandleFunc("/login/otp", func(w http.ResponseWriter, r *http.Request) { write(w, "Sign in", oneStep("code", "")) })
	mux.HandleFunc("/login/home", func(w http.ResponseWriter, r *http.Request) { write(w, "Home", "<h1>Welcome back</h1>") })
	mux.HandleFunc("/login/check", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if r.PostFormValue("email") != loginUser || r.PostFormValue("password") != loginPassword {
			write(w, "Sign in", oneStep(html.EscapeString(r.PostFormValue("then")), wrong))
			return
		}
		if r.PostFormValue("then") == "code" {
			write(w, "Verify", codePage(""))
			return
		}
		http.Redirect(w, r, "/login/home", http.StatusSeeOther)
	})
	mux.HandleFunc("/login/code", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if r.PostFormValue("code") != loginCode {
			write(w, "Verify", codePage(`<p role="alert">Wrong code</p>`))
			return
		}
		http.Redirect(w, r, "/login/home", http.StatusSeeOther)
	})
	// Two steps, as the big sites do: the address on one page, the password on the next.
	mux.HandleFunc("/login/two", func(w http.ResponseWriter, r *http.Request) {
		write(w, "Sign in", `<form method="post" action="/login/two/password"><p><label>Email or phone <input name="identifier"></label></p><button>Next</button></form>`)
	})
	mux.HandleFunc("/login/two/password", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		write(w, "Sign in", `<form method="post" action="/login/check"><input type="hidden" name="email" value="`+html.EscapeString(r.PostFormValue("identifier"))+`">`+
			`<p><label>Password <input name="password" type="password"></label></p><button>Sign in</button></form>`)
	})
	// Fields that say nothing of what they are, and a "button" that is a div: only selectors find them.
	mux.HandleFunc("/login/odd", func(w http.ResponseWriter, r *http.Request) {
		write(w, "Odd", `<form id="f" method="post" action="/login/odd/check"><input id="f1" name="a"> <input id="f2" name="b" style="-webkit-text-security:disc">`+
			`<div id="go" role="button" tabindex="0" style="display:inline-block;padding:4px 12px;border:1px solid" onclick="document.getElementById('f').submit()">Go</div></form>`)
	})
	mux.HandleFunc("/login/odd/check", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if r.PostFormValue("a") == loginUser && r.PostFormValue("b") == loginPassword {
			http.Redirect(w, r, "/login/home", http.StatusSeeOther)
			return
		}
		write(w, "Odd", wrong)
	})
	// One page that shows the password field once the address is in, and never navigates.
	mux.HandleFunc("/login/spa", func(w http.ResponseWriter, r *http.Request) {
		write(w, "Sign in", `<form id="f"><p><label>Email <input name="email" type="email"></label></p>`+
			`<div id="pw" hidden><p><label>Password <input name="password" type="password"></label></p></div><button>Continue</button></form>`+
			`<script>document.getElementById('f').onsubmit = (e) => { e.preventDefault(); const pw = document.getElementById('pw');`+
			`if (pw.hidden) { pw.hidden = false; return; } history.pushState({}, '', '/login/home'); document.body.innerHTML = '<h1>Welcome</h1>'; document.title = 'Home'; };</script>`)
	})
}

type loginResult struct {
	URL      string          `json:"url"`
	SignedIn string          `json:"signed_in"`
	Filled   []string        `json:"filled"`
	Found    map[string]bool `json:"found"`
	Waiting  string          `json:"waiting"`
	Stopped  string          `json:"stopped"`
}

func (h *harness) login(params map[string]any) (loginResult, string, error) {
	var raw json.RawMessage
	if _, ok := params["site"]; !ok {
		params["site"] = "127.0.0.1"
	}
	if _, ok := params["origin"]; !ok {
		params["origin"] = map[string]any{"actor": "operator"}
	}
	err := h.call("page.login", params, &raw)
	var r loginResult
	if err == nil {
		if e := json.Unmarshal(raw, &r); e != nil {
			h.t.Fatal(e)
		}
	}
	return r, string(raw), err
}

// noLoginValue fails when a value of the sign-in is anywhere the daemon says things: a reply, an
// event, its log.
func (h *harness) noLoginValue(where string, values ...string) {
	h.t.Helper()
	evs, _ := h.evlog.After(0, 20000)
	events, _ := json.Marshal(evs)
	for _, v := range append(values, loginPassword, loginCode) {
		if strings.Contains(where, v) {
			h.t.Fatalf("a sign-in value came back: %s", where)
		}
		if strings.Contains(string(events), v) {
			h.t.Fatalf("a sign-in value reached an event")
		}
		if strings.Contains(h.logged.String(), v) {
			h.t.Fatalf("a sign-in value reached the log")
		}
	}
}

func TestLoginOneStepSignsInAndNoValueLeaves(t *testing.T) {
	h := start(t, nil)
	o := h.open("l1", "login-profile", "/login/one")
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword})
	if err != nil {
		t.Fatal(err)
	}
	if r.SignedIn != "yes" || !strings.HasSuffix(r.URL, "/login/home") || strings.Join(r.Filled, ",") != "username,password" {
		t.Fatalf("result: %s", raw)
	}
	h.noLoginValue(raw, loginUser)
}

func TestLoginWrongPasswordIsANo(t *testing.T) {
	h := start(t, nil)
	o := h.open("l2", "login-profile", "/login/one")
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": "SECRET-not-it"})
	if err != nil {
		t.Fatal(err)
	}
	if r.SignedIn != "no" {
		t.Fatalf("a refused password was not a no: %s", raw)
	}
	h.noLoginValue(raw, "SECRET-not-it")
}

func TestLoginTwoStepsTypesThePasswordOnTheNextPage(t *testing.T) {
	h := start(t, nil)
	o := h.open("l3", "login-profile", "/login/two")
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword})
	if err != nil {
		t.Fatal(err)
	}
	if r.SignedIn != "yes" || strings.Join(r.Filled, ",") != "username,password" {
		t.Fatalf("result: %s", raw)
	}
	h.noLoginValue(raw)
}

func TestLoginAsksForTheCodeAndTakesItNext(t *testing.T) {
	h := start(t, nil)
	o := h.open("l4", "login-profile", "/login/otp")
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword})
	if err != nil {
		t.Fatal(err)
	}
	if r.SignedIn != "unknown" || r.Waiting != "code" {
		t.Fatalf("the code page was not said: %s", raw)
	}
	r, raw, err = h.login(map[string]any{"tab_id": o.Tab.ID, "otp": loginCode})
	if err != nil {
		t.Fatal(err)
	}
	if r.SignedIn != "yes" || strings.Join(r.Filled, ",") != "otp" {
		t.Fatalf("result: %s", raw)
	}
	h.noLoginValue(raw)
}

func TestLoginSelectorsFindWhatTheGuessCannot(t *testing.T) {
	h := start(t, nil)
	o := h.open("l5", "login-profile", "/login/odd")
	_, _, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword})
	if code(err) != 1001 || !strings.Contains(err.Error(), "no sign-in form") {
		t.Fatalf("a page without a recognisable form: %v", err)
	}
	_, _, err = h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "selectors": map[string]any{"username": "#nothing"}})
	if code(err) != -32602 || !strings.Contains(err.Error(), "username: #nothing") {
		t.Fatalf("a selector that matches nothing: %v", err)
	}
	sel := map[string]any{"username": "#f1", "password": "#f2", "submit": "#go"}
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword, "selectors": sel})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(r.URL, "/login/home") || strings.Join(r.Filled, ",") != "username,password" {
		t.Fatalf("result: %s", raw)
	}
	h.noLoginValue(raw)
}

func TestLoginTypesNothingOnAnotherSite(t *testing.T) {
	h := start(t, nil)
	o := h.open("l6", "login-profile", "/login/one")
	_, _, err := h.login(map[string]any{"tab_id": o.Tab.ID, "site": "example.com", "username": loginUser, "password": loginPassword})
	if code(err) != 1004 || !strings.Contains(err.Error(), "not example.com; nothing was typed") {
		t.Fatalf("another site: %v", err)
	}
	// A look-alike that ends in the site's name is another site.
	if _, _, err := h.login(map[string]any{"tab_id": o.Tab.ID, "site": "0.0.1", "username": loginUser}); code(err) != 1004 {
		t.Fatalf("a suffix that is not a parent domain: %v", err)
	}
}

func TestLoginMarksTheUsernameSecretAndProbesFirst(t *testing.T) {
	h := start(t, nil)
	o := h.open("l7", "login-profile", "/still")
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "probe": true})
	if err != nil || r.Found["username"] || r.Found["password"] {
		t.Fatalf("a page with no form: %s %v", raw, err)
	}
	var nav struct{}
	h.must("page.navigate", map[string]any{"tab_id": o.Tab.ID, "url": h.site.URL + "/login/spa"}, &nav)
	r, raw, err = h.login(map[string]any{"tab_id": o.Tab.ID, "probe": true})
	if err != nil || !r.Found["username"] || r.Found["password"] || !r.Found["submit"] {
		t.Fatalf("the probe: %s %v", raw, err)
	}
	// The address alone: the page then shows its password field, which nothing was given for.
	r, raw, err = h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser})
	if err != nil || r.SignedIn != "unknown" || !r.Found["password"] {
		t.Fatalf("after the address: %s %v", raw, err)
	}
	var snap struct {
		Text string `json:"text"`
	}
	h.must("page.snapshot", map[string]any{"tab_id": o.Tab.ID}, &snap)
	if strings.Contains(snap.Text, loginUser) || !strings.Contains(snap.Text, `textbox "Email"`) {
		t.Fatalf("the outline after the sign-in: %s", snap.Text)
	}
	h.noLoginValue(raw+snap.Text, loginUser)
}

func TestLoginPassesThePauseOnlyForTheOperator(t *testing.T) {
	h := start(t, nil)
	o := h.open("l8", "login-profile", "/login/one")
	h.must("control.set", map[string]any{"group_id": "l8", "owner": "paused", "reason": "login: sign in"}, nil)
	if _, _, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword, "origin": map[string]any{"actor": "agent"}}); err == nil {
		t.Fatal("the agent signed in through a pause")
	}
	r, raw, err := h.login(map[string]any{"tab_id": o.Tab.ID, "username": loginUser, "password": loginPassword})
	if err != nil || r.SignedIn != "yes" {
		t.Fatalf("the operator's sign-in while paused: %s %v", raw, err)
	}
}
