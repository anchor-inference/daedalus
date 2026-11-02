package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strconv"
)

// NewSession is the body of POST /api/sessions as this client sends it.
type NewSession struct {
	Title     string `json:"title,omitempty"`
	Autotitle bool   `json:"autotitle,omitempty"`
	Prompt    string `json:"prompt,omitempty"`
	// Kind is "remote" for a session born here; DeviceID and Cwd bind its tools to this machine's
	// folder through the device connection.
	Kind     string `json:"kind,omitempty"`
	DeviceID string `json:"device_id,omitempty"`
	Cwd      string `json:"cwd,omitempty"`
	// Guard is the local guard the session starts under.
	Guard  string `json:"guard,omitempty"`
	Preset string `json:"preset,omitempty"`
}

// SessionRef is what creating a session answers.
type SessionRef struct {
	ID    string `json:"id"`
	Title string `json:"title"`
	Model string `json:"model"`
	Kind  string `json:"kind"`
}

// SessionRow is one entry of the session list.
type SessionRow struct {
	ID            string         `json:"id"`
	Title         string         `json:"title"`
	Status        string         `json:"status"`
	Kind          string         `json:"kind"`
	CreatedAt     string         `json:"created_at"`
	LastMessageAt string         `json:"last_message_at"`
	RunID         *string        `json:"run_id"`
	Metadata      map[string]any `json:"metadata"`
}

// Message is one row of a transcript page.
type Message struct {
	Seq       *int64 `json:"seq"`
	RunID     string `json:"run_id"`
	Role      string `json:"role"`
	Text      string `json:"text"`
	Thinking  string `json:"thinking"`
	Origin    string `json:"origin"`
	Internal  bool   `json:"internal"`
	Summary   bool   `json:"summary"`
	CreatedAt string `json:"created_at"`
	ToolCalls []struct {
		ID        string         `json:"id"`
		Name      string         `json:"name"`
		Arguments map[string]any `json:"arguments"`
	} `json:"tool_calls"`
	ToolResults []struct {
		ID      string `json:"id"`
		Content string `json:"content"`
		IsError bool   `json:"is_error"`
	} `json:"tool_results"`
	ClientMessageID string `json:"client_message_id"`
}

// Question is one item of a pending AskUser.
type Question struct {
	Header   string `json:"header"`
	Question string `json:"question"`
	Options  []struct {
		Label       string `json:"label"`
		Description string `json:"description"`
	} `json:"options"`
	MultiSelect bool `json:"multiSelect"`
}

// SessionDetail is the part of GET /api/sessions/{id} this client reads.
type SessionDetail struct {
	ID            string    `json:"id"`
	Title         string    `json:"title"`
	Status        string    `json:"status"`
	Error         string    `json:"error"`
	RunID         *string   `json:"run_id"`
	Kind          string    `json:"kind"`
	Model         string    `json:"model"`
	Workspace     string    `json:"workspace"`
	Env           string    `json:"env"`
	Messages      []Message `json:"messages"`
	OpenApprovals []string  `json:"open_approvals"`
	Pending       *struct {
		ID        string     `json:"id"`
		Questions []Question `json:"questions"`
	} `json:"pending"`
	Usage   map[string]any `json:"usage"`
	Context map[string]any `json:"context"`
	// Remote is the home device and folder of a remote session; nil for any other.
	Remote *RemoteInfo `json:"remote"`
}

// Running reports whether a turn is in flight.
func (d *SessionDetail) Running() bool { return d.Status == "running" || d.Status == "compacting" }

func sessionPath(id string, rest string) string { return "/api/sessions/" + url.PathEscape(id) + rest }

// CreateSession opens a session.
func (c *Client) CreateSession(ctx context.Context, body NewSession) (SessionRef, error) {
	var out SessionRef
	err := c.Do(ctx, http.MethodPost, "/api/sessions", nil, body, &out)
	return out, err
}

// Session reads a session with its newest tail turns.
func (c *Client) Session(ctx context.Context, id string, tail int) (*SessionDetail, error) {
	q := url.Values{}
	if tail > 0 {
		q.Set("tail", strconv.Itoa(tail))
	}
	var out SessionDetail
	if err := c.Do(ctx, http.MethodGet, sessionPath(id, ""), q, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Sessions lists the most recent sessions.
func (c *Client) Sessions(ctx context.Context, limit int) ([]SessionRow, error) {
	q := url.Values{}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	var out struct {
		Sessions []SessionRow `json:"sessions"`
	}
	err := c.Do(ctx, http.MethodGet, "/api/sessions", q, nil, &out)
	return out.Sessions, err
}

// SendBody is one message to a session.
type SendBody struct {
	Text     string `json:"text"`
	Steer    bool   `json:"steer,omitempty"`
	FollowUp bool   `json:"follow_up,omitempty"`
	// ClientMessageID makes a retried send land once.
	ClientMessageID string `json:"client_message_id,omitempty"`
}

// Send delivers a message and answers the run it started or joined.
func (c *Client) Send(ctx context.Context, id string, body SendBody) (string, error) {
	var out struct {
		RunID string `json:"run_id"`
	}
	err := c.Do(ctx, http.MethodPost, sessionPath(id, "/messages"), nil, body, &out)
	return out.RunID, err
}

// Stop interrupts the running turn.
func (c *Client) Stop(ctx context.Context, id string) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/stop"), nil, struct{}{}, nil)
}

// AnswerItem is the answer to one question.
type AnswerItem struct {
	Question string   `json:"question"`
	Selected []string `json:"selected"`
	Custom   *string  `json:"custom"`
}

// Answer answers the pending AskUser.
func (c *Client) Answer(ctx context.Context, id string, answers []AnswerItem) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/answer"), nil, map[string]any{"answers": answers}, nil)
}

// Grant lets one refused call through; GrantSimilar the calls like it; Refuse leaves it refused.
func (c *Client) Grant(ctx context.Context, id, key string) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/policy/grant"), nil, map[string]string{"key": key}, nil)
}

func (c *Client) GrantSimilar(ctx context.Context, id, key string) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/policy/grant-similar"), nil, map[string]string{"key": key}, nil)
}

func (c *Client) Refuse(ctx context.Context, id, key string) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/policy/refuse"), nil, map[string]string{"key": key}, nil)
}

// Similar is what "allow similar" would cover for an open request, empty when nothing narrower.
func (c *Client) Similar(ctx context.Context, id, key string) (string, error) {
	var out struct {
		Similar any `json:"similar"`
	}
	if err := c.Do(ctx, http.MethodGet, sessionPath(id, "/policy/similar/"+url.PathEscape(key)), nil, nil, &out); err != nil {
		return "", err
	}
	switch v := out.Similar.(type) {
	case nil:
		return "", nil
	case string:
		return v, nil
	case map[string]any:
		if s, ok := v["label"].(string); ok {
			return s, nil
		}
		raw, _ := json.Marshal(v)
		return string(raw), nil
	}
	return fmt.Sprint(out.Similar), nil
}

// SetModel switches the session's model or effort.
func (c *Client) SetModel(ctx context.Context, id string, body map[string]any) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/model"), nil, body, nil)
}

// Compact summarises the history.
func (c *Client) Compact(ctx context.Context, id, instructions string) error {
	// instructions is the one field the server reads, and what the app sends too.
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/compact"), nil, map[string]string{"instructions": instructions}, nil)
}

// Command is one server slash command.
type Command struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Args        string `json:"args"`
	Scope       string `json:"scope"`
	Confirm     bool   `json:"confirm"`
}

// Commands lists the server's commands.
func (c *Client) Commands(ctx context.Context) ([]Command, error) {
	var out []Command
	err := c.Do(ctx, http.MethodGet, "/api/commands", nil, nil, &out)
	return out, err
}

// Skill is one enabled skill, offered in the palette by name.
type Skill struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

// Skills lists the enabled skills.
func (c *Client) Skills(ctx context.Context) ([]Skill, error) {
	var out []Skill
	err := c.Do(ctx, http.MethodGet, "/api/skills", nil, nil, &out)
	return out, err
}

// RunCommand runs one server command line in a session and answers the text the chat would show.
func (c *Client) RunCommand(ctx context.Context, id, line string) (string, error) {
	var out struct {
		Text string `json:"text"`
	}
	err := c.Do(ctx, http.MethodPost, sessionPath(id, "/command"), nil, map[string]string{"line": line}, &out)
	return out.Text, err
}

// Revert cuts the conversation back to before the message at seq.
func (c *Client) Revert(ctx context.Context, id string, seq int64) error {
	return c.Do(ctx, http.MethodPost, sessionPath(id, "/revert"), nil, map[string]int64{"seq": seq}, nil)
}

// Queued is a message the server holds until the agent reads it.
type Queued struct {
	ID   string `json:"id"`
	Text string `json:"text"`
}

// Queue lists the messages the session holds for the agent.
func (c *Client) Queue(ctx context.Context, id string) ([]Queued, error) {
	var out []Queued
	err := c.Do(ctx, http.MethodGet, sessionPath(id, "/steer"), nil, nil, &out)
	return out, err
}

// Withdraw takes a queued message back.
func (c *Client) Withdraw(ctx context.Context, id, messageID string) (string, error) {
	var out struct {
		Text string `json:"text"`
	}
	err := c.Do(ctx, http.MethodDelete, sessionPath(id, "/steer/"+url.PathEscape(messageID)), nil, nil, &out)
	return out.Text, err
}

// Upload attaches a file to the session's next message.
func (c *Client) Upload(ctx context.Context, id, name, mime string, data []byte) error {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	part, err := w.CreateFormFile("file", name)
	if err != nil {
		return err
	}
	if _, err := part.Write(data); err != nil {
		return err
	}
	if err := w.Close(); err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url(sessionPath(id, "/upload"), nil), &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", w.FormDataContentType())
	c.authorize(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return readAPIError(resp)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	return nil
}
