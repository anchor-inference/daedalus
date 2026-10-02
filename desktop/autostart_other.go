//go:build !windows

package main

import "errors"

type unavailableRunKey struct{}

func (unavailableRunKey) Get(string) (string, bool, error) {
	return "", false, errors.New("the registry is only on Windows")
}
func (unavailableRunKey) Set(string, string) error {
	return errors.New("the registry is only on Windows")
}
func (unavailableRunKey) Delete(string) error { return errors.New("the registry is only on Windows") }

func systemRunKey() runKey { return unavailableRunKey{} }
