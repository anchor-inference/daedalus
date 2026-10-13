package rpc

import (
	"context"
	"encoding/json"
	"sort"
	"strings"

	"github.com/ascorblack/daedalus/browserd/internal/browser"
	"github.com/ascorblack/daedalus/browserd/internal/config"
	"github.com/ascorblack/daedalus/ptyd/proto/server"
	"github.com/ascorblack/daedalus/ptyd/proto/wire"
)

// The cookies of a group's browser: set from a file the operator handed over, listed, cleared for a
// site. They are written through the browser itself (Storage on the browser's own session, in the
// group's context for a throwaway group), so they land in the profile's own cookie store on disk and
// survive the browser closing, as a login made by hand does.
//
// A value goes in and never comes out: cookies.list answers names, domains and expiry, and a failure
// names the cookie by name and domain. The host asks for cookies so the agent can be signed in, not
// so it can read a session's credentials (redact.go cuts the same values from the page's traffic).

const (
	maxCookies     = 3000 // more than any real export; a bigger list is a mistake, not a jar
	maxCookieValue = 4096 // the most a browser stores of one cookie
)

// cookieParam is the DevTools protocol's Network.CookieParam, the parts of it the host sends.
type cookieParam struct {
	Name     string   `json:"name"`
	Value    string   `json:"value"`
	URL      string   `json:"url,omitempty"`
	Domain   string   `json:"domain,omitempty"`
	Path     string   `json:"path,omitempty"`
	Secure   bool     `json:"secure,omitempty"`
	HTTPOnly bool     `json:"httpOnly,omitempty"`
	SameSite string   `json:"sameSite,omitempty"`
	Expires  *float64 `json:"expires,omitempty"`
}

// cookieView is a cookie as it may leave the daemon: everything but its value.
type cookieView struct {
	Name     string  `json:"name"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"` // seconds since the epoch; 0 for a session cookie
	Secure   bool    `json:"secure"`
	HTTPOnly bool    `json:"http_only"`
	SameSite string  `json:"same_site,omitempty"`
}

// storedCookie is Network.Cookie as Storage.getCookies answers it.
type storedCookie struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	Secure   bool    `json:"secure"`
	HTTPOnly bool    `json:"httpOnly"`
	Session  bool    `json:"session"`
	SameSite string  `json:"sameSite"`
}

func (d *Daemon) registerCookies(s *server.Server) {
	s.Handle("cookies.set", d.cookiesSet)
	s.Handle("cookies.list", d.cookiesList)
	s.Handle("cookies.clear", d.cookiesClear)
}

// cookieGroup resolves the group and lets the call through its control, as an action on a page.
func (d *Daemon) cookieGroup(ctx context.Context, id string, o *browser.Origin) (*browser.Group, error) {
	g, err := d.Manager.Group(id)
	if err != nil {
		return nil, err
	}
	if err := g.Gate(ctx, o, config.ControlWait); err != nil {
		return nil, err
	}
	return g, nil
}

// contextOf adds what Storage takes to reach a throwaway group's own jar; a profile's browser has one.
func contextOf(g *browser.Group, params map[string]any) map[string]any {
	if g.ContextID != "" {
		params["browserContextId"] = g.ContextID
	}
	return params
}

func checkCookie(c cookieParam) string {
	switch {
	case c.Name == "" && c.Value == "":
		return "no name"
	case c.URL == "" && c.Domain == "":
		return "no domain"
	case len(c.Value) > maxCookieValue:
		return "value longer than a browser keeps"
	case c.SameSite != "" && c.SameSite != "Strict" && c.SameSite != "Lax" && c.SameSite != "None":
		return "sameSite is not Strict, Lax or None"
	case c.URL != "" && !strings.HasPrefix(c.URL, "http://") && !strings.HasPrefix(c.URL, "https://"):
		return "url is not http or https"
	}
	return ""
}

func (d *Daemon) cookiesSet(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		Cookies []cookieParam   `json:"cookies"`
		Origin  *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if len(p.Cookies) == 0 || len(p.Cookies) > maxCookies {
		return nil, wire.Errorf(wire.CodeInvalidParams, "cookies must hold 1-%d cookies", maxCookies)
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	type rejected struct {
		Name   string `json:"name"`
		Domain string `json:"domain"`
		Reason string `json:"reason"`
	}
	rejects := []rejected{}
	set := 0
	// One at a time: Storage.setCookies refuses the whole list for one cookie the browser will not
	// take, and an export of a few hundred carries the odd one; the rest still go in, and the one
	// refused is named.
	for _, ck := range p.Cookies {
		where := ck.Domain
		if where == "" {
			where = ck.URL
		}
		if why := checkCookie(ck); why != "" {
			rejects = append(rejects, rejected{Name: ck.Name, Domain: where, Reason: why})
			continue
		}
		err := g.Browser.Conn().Call(ctx, "", "Storage.setCookies", contextOf(g, map[string]any{"cookies": []cookieParam{ck}}), nil)
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			// The browser's own words name the field it refused, never the value.
			rejects = append(rejects, rejected{Name: ck.Name, Domain: where, Reason: "the browser refused it: " + err.Error()})
			continue
		}
		set++
	}
	g.Touch()
	d.Log.Info("cookies set", "group", g.ID, "set", set, "rejected", len(rejects))
	return map[string]any{"set": set, "rejected": rejects}, nil
}

func (d *Daemon) storedCookies(ctx context.Context, g *browser.Group) ([]storedCookie, error) {
	var r struct {
		Cookies []storedCookie `json:"cookies"`
	}
	if err := g.Browser.Conn().Call(ctx, "", "Storage.getCookies", contextOf(g, map[string]any{}), &r); err != nil {
		return nil, err
	}
	return r.Cookies, nil
}

// onDomain says a cookie's domain is the site asked for or under it: "wolt.com" holds the cookies
// of .wolt.com and of www.wolt.com.
func onDomain(cookieDomain, site string) bool {
	site = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(site)), ".")
	if site == "" {
		return true
	}
	cd := strings.TrimPrefix(strings.ToLower(cookieDomain), ".")
	return cd == site || strings.HasSuffix(cd, "."+site)
}

func (d *Daemon) cookiesList(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string `json:"group_id"`
		Domain  string `json:"domain,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	g, err := d.Manager.Group(p.GroupID)
	if err != nil {
		return nil, err
	}
	stored, err := d.storedCookies(ctx, g)
	if err != nil {
		return nil, err
	}
	out := []cookieView{}
	for _, ck := range stored {
		if !onDomain(ck.Domain, p.Domain) {
			continue
		}
		expires := ck.Expires
		if ck.Session || expires < 0 {
			expires = 0
		}
		out = append(out, cookieView{Name: ck.Name, Domain: ck.Domain, Path: ck.Path, Expires: expires, Secure: ck.Secure, HTTPOnly: ck.HTTPOnly, SameSite: ck.SameSite})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Domain != out[j].Domain {
			return out[i].Domain < out[j].Domain
		}
		return out[i].Name < out[j].Name
	})
	return map[string]any{"cookies": out}, nil
}

func (d *Daemon) cookiesClear(ctx context.Context, c *server.Conn, params json.RawMessage) (any, error) {
	var p struct {
		GroupID string          `json:"group_id"`
		Domain  string          `json:"domain"`
		Origin  *browser.Origin `json:"origin,omitempty"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	// Every cookie of the profile at once is profile.clear's, with the browser closed; this is a site.
	if strings.Trim(strings.TrimSpace(p.Domain), ".") == "" {
		return nil, wire.Errorf(wire.CodeInvalidParams, "domain is required: the site whose cookies go")
	}
	g, err := d.cookieGroup(ctx, p.GroupID, p.Origin)
	if err != nil {
		return nil, err
	}
	t := g.ActiveTab()
	if t == nil {
		return nil, wire.Errorf(wire.CodeNotFound, "the group has no tab to clear cookies through")
	}
	stored, err := d.storedCookies(ctx, g)
	if err != nil {
		return nil, err
	}
	cleared := 0
	// Network.deleteCookies on a tab's session, which reaches the jar of the tab's context; Storage
	// has no way to delete one cookie, only every cookie at once.
	for _, ck := range stored {
		if !onDomain(ck.Domain, p.Domain) {
			continue
		}
		if err := t.Call(ctx, "Network.deleteCookies", map[string]any{"name": ck.Name, "domain": ck.Domain, "path": ck.Path}, nil); err != nil {
			return nil, err
		}
		cleared++
	}
	g.Touch()
	return map[string]any{"cleared": cleared}, nil
}
