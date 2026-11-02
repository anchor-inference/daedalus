package auth

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"runtime"
	"time"

	"github.com/ascorblack/daedalus/cli/internal/client"
)

// DeviceInfo is what pairing tells the server about this machine.
type DeviceInfo struct {
	Name          string `json:"name,omitempty"`
	Hostname      string `json:"hostname,omitempty"`
	OS            string `json:"os"`
	Arch          string `json:"arch"`
	ClientVersion string `json:"client_version"`
}

// ThisDevice describes this machine, named name or after its host name.
func ThisDevice(name, version string) DeviceInfo {
	host, _ := os.Hostname()
	if name == "" {
		name = host
	}
	return DeviceInfo{Name: name, Hostname: host, OS: runtime.GOOS, Arch: runtime.GOARCH, ClientVersion: version}
}

// Challenge is a started pairing: what the operator compares and where they approve it.
type Challenge struct {
	DeviceCode              string `json:"device_code"`
	UserCode                string `json:"user_code"`
	VerificationURI         string `json:"verification_uri"`
	VerificationURIComplete string `json:"verification_uri_complete"`
	ExpiresIn               int    `json:"expires_in"`
	Interval                int    `json:"interval"`
}

// Expires is when the challenge stops being redeemable.
func (c Challenge) Expires(from time.Time) time.Time {
	return from.Add(time.Duration(c.ExpiresIn) * time.Second)
}

// Pairing outcomes other than approval.
var (
	ErrDenied  = errors.New("the operator refused this device")
	ErrExpired = errors.New("the pairing request expired before anyone approved it")
	ErrUnknown = errors.New("the server does not know this pairing request")
	ErrTooMany = errors.New("too many pairing attempts; wait a few minutes and try again")
)

// Start asks the server for a pairing code. It needs no credential.
func Start(ctx context.Context, c *client.Client, info DeviceInfo) (Challenge, error) {
	var out Challenge
	if err := c.WithToken("").Do(ctx, http.MethodPost, "/api/cli/pair/start", nil, info, &out); err != nil {
		var api *client.APIError
		if errors.As(err, &api) && api.Status == http.StatusTooManyRequests {
			return out, ErrTooMany
		}
		return out, err
	}
	if out.DeviceCode == "" || out.UserCode == "" {
		return out, errors.New("the server's pairing answer has no code")
	}
	if out.Interval <= 0 {
		out.Interval = 3
	}
	return out, nil
}

// Approved is a finished pairing.
type Approved struct {
	Token  string        `json:"token"`
	Device client.Device `json:"device"`
}

// Wait polls until the operator decides, the request expires, or ctx ends. The token is in the one
// answer that reports the approval and nowhere else, so the caller must store it before anything
// else can go wrong.
func Wait(ctx context.Context, c *client.Client, ch Challenge) (Approved, error) {
	interval := time.Duration(ch.Interval) * time.Second
	deadline := time.Now().Add(time.Duration(ch.ExpiresIn)*time.Second + 30*time.Second)
	anon := c.WithToken("")
	for {
		select {
		case <-ctx.Done():
			return Approved{}, ctx.Err()
		case <-time.After(interval):
		}
		if ch.ExpiresIn > 0 && time.Now().After(deadline) {
			return Approved{}, ErrExpired
		}
		var out struct {
			Status   string        `json:"status"`
			Interval int           `json:"interval"`
			Token    string        `json:"token"`
			Device   client.Device `json:"device"`
		}
		err := anon.Do(ctx, http.MethodPost, "/api/cli/pair/poll", nil, map[string]string{"device_code": ch.DeviceCode}, &out)
		if err != nil {
			var api *client.APIError
			if errors.As(err, &api) {
				switch api.Status {
				case http.StatusNotFound:
					return Approved{}, ErrUnknown
				case http.StatusTooManyRequests:
					return Approved{}, ErrTooMany
				}
				if api.Status < 500 {
					return Approved{}, err
				}
			}
			// A server restarting or a network hiccup: keep polling until the request expires.
			continue
		}
		if out.Interval > 0 {
			interval = time.Duration(out.Interval) * time.Second
		}
		switch out.Status {
		case "pending", "slow_down":
		case "approved":
			if out.Token == "" {
				return Approved{}, errors.New("the server approved the device and sent no token")
			}
			return Approved{Token: out.Token, Device: out.Device}, nil
		case "denied":
			return Approved{}, ErrDenied
		case "expired":
			return Approved{}, ErrExpired
		default:
			return Approved{}, fmt.Errorf("the server answered the poll with %q", out.Status)
		}
	}
}
