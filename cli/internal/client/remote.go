package client

import (
	"context"
	"net/http"
	"net/url"
	"strconv"
)

// The routes of docs/architecture/cli.md that exist for this client: who the device is, the sessions
// it may open, its guard, and its presence.

// Device is a paired device as the server describes it.
type Device struct {
	ID            string  `json:"id"`
	Name          string  `json:"name"`
	Hostname      string  `json:"hostname"`
	OS            string  `json:"os"`
	Arch          string  `json:"arch"`
	ClientVersion string  `json:"client_version"`
	PairedAt      string  `json:"paired_at"`
	ApprovedVia   string  `json:"approved_via"`
	LastSeenAt    string  `json:"last_seen_at"`
	ExpiresAt     string  `json:"expires_at"`
	RevokedAt     *string `json:"revoked_at"`
	Connected     bool    `json:"connected"`
	Sessions      int     `json:"sessions"`
}

// Me is GET /api/cli/me.
type Me struct {
	Device Device `json:"device"`
	Server struct {
		Version   string `json:"version"`
		Contract  int    `json:"contract"`
		PublicURL string `json:"public_url"`
	} `json:"server"`
	Sessions []string `json:"sessions"`
}

// Me asks the server who this device is.
func (c *Client) Me(ctx context.Context) (Me, error) {
	var out Me
	err := c.Do(ctx, http.MethodGet, "/api/cli/me", nil, nil, &out)
	return out, err
}

// RemoteInfo is a session's `remote` field: its home device and folder.
type RemoteInfo struct {
	DeviceID   string `json:"device_id"`
	DeviceName string `json:"device_name"`
	Cwd        string `json:"cwd"`
	Connected  bool   `json:"connected"`
	Guard      string `json:"guard"`
}

// ResumeRow is one row of GET /api/cli/sessions.
type ResumeRow struct {
	ID            string      `json:"id"`
	Title         string      `json:"title"`
	Kind          string      `json:"kind"`
	Status        string      `json:"status"`
	LastMessageAt string      `json:"last_message_at"`
	CreatedAt     string      `json:"created_at"`
	Remote        *RemoteInfo `json:"remote"`
	Attached      bool        `json:"attached"`
	Here          bool        `json:"here"`
}

// ResumeSessions lists the sessions /resume offers, newest first; cwd marks the ones whose home is
// this folder, q filters by title.
func (c *Client) ResumeSessions(ctx context.Context, cwd, q string, limit int) ([]ResumeRow, error) {
	query := url.Values{}
	if cwd != "" {
		query.Set("cwd", cwd)
	}
	if q != "" {
		query.Set("q", q)
	}
	if limit > 0 {
		query.Set("limit", strconv.Itoa(limit))
	}
	var out struct {
		Sessions []ResumeRow `json:"sessions"`
	}
	err := c.Do(ctx, http.MethodGet, "/api/cli/sessions", query, nil, &out)
	return out.Sessions, err
}

// Attach puts a session into this device's scope.
func (c *Client) Attach(ctx context.Context, id string) (ResumeRow, error) {
	var out ResumeRow
	err := c.Do(ctx, http.MethodPost, "/api/cli/sessions/"+url.PathEscape(id)+"/attach", nil, struct{}{}, &out)
	return out, err
}

// Hands makes this device and folder the home of a remote session.
func (c *Client) Hands(ctx context.Context, id, cwd string) (ResumeRow, error) {
	var out ResumeRow
	err := c.Do(ctx, http.MethodPost, "/api/cli/sessions/"+url.PathEscape(id)+"/hands", nil, map[string]string{"cwd": cwd}, &out)
	return out, err
}

// SetGuard reports the local guard of a session.
func (c *Client) SetGuard(ctx context.Context, id, guard string) error {
	return c.Do(ctx, http.MethodPut, "/api/cli/sessions/"+url.PathEscape(id)+"/guard", nil, map[string]string{"guard": guard}, nil)
}

// Preset is one model preset.
type Preset struct {
	ID              string `json:"id"`
	Label           string `json:"label"`
	Provider        string `json:"provider"`
	Model           string `json:"model"`
	ReasoningEffort string `json:"reasoning_effort"`
	ContextWindow   int    `json:"context_window"`
}

// Models is GET /api/models.
type Models struct {
	Default string   `json:"default"`
	Presets []Preset `json:"presets"`
	Efforts []string `json:"efforts"`
}

// Models lists the presets the server offers.
func (c *Client) Models(ctx context.Context) (Models, error) {
	var out Models
	err := c.Do(ctx, http.MethodGet, "/api/models", nil, nil, &out)
	return out, err
}

// Presence is one report of what this terminal shows.
type Presence struct {
	Client   string   `json:"client"`
	Kind     string   `json:"kind"`
	Visible  bool     `json:"visible"`
	Focused  bool     `json:"focused"`
	Sessions []string `json:"sessions"`
	Lang     string   `json:"lang,omitempty"`
	TZ       string   `json:"tz,omitempty"`
}

// ReportPresence tells the server what this terminal shows.
func (c *Client) ReportPresence(ctx context.Context, p Presence) error {
	p.Kind = "cli"
	if p.Sessions == nil {
		p.Sessions = []string{}
	}
	return c.Do(ctx, http.MethodPost, "/api/presence", nil, p, nil)
}
