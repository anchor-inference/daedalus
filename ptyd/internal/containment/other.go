//go:build !linux

package containment

import (
	"fmt"
	"runtime"
)

type Controller struct{}

func NewController(_ string) (*Controller, error) {
	return &Controller{}, nil
}

func (c *Controller) Probe() Status {
	return Status{Kind: "unsupported", Reason: fmt.Sprintf("hard attempt ceilings are not verified on %s", runtime.GOOS)}
}

func (c *Controller) Claim(_ Scope, _ Limits) (Handle, error) {
	return nil, ErrUnavailable
}

func (c *Controller) Preflight(_ Limits) error {
	return ErrUnavailable
}

func (c *Controller) Lookup(_ Scope) (Handle, error) {
	return nil, ErrUnavailable
}

func (c *Controller) Release(_ Scope) error {
	return ErrUnavailable
}
