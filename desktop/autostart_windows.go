//go:build windows

package main

import (
	"errors"

	"golang.org/x/sys/windows/registry"
)

type registryRunKey struct{}

func systemRunKey() runKey { return registryRunKey{} }

const runKeySubpath = `Software\Microsoft\Windows\CurrentVersion\Run`

func (registryRunKey) Get(name string) (string, bool, error) {
	key, err := registry.OpenKey(registry.CURRENT_USER, runKeySubpath, registry.QUERY_VALUE)
	if errors.Is(err, registry.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	defer key.Close()
	value, _, err := key.GetStringValue(name)
	if errors.Is(err, registry.ErrNotExist) {
		return "", false, nil
	}
	return value, err == nil, err
}

func (registryRunKey) Set(name, value string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, runKeySubpath, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	return key.SetStringValue(name, value)
}

func (registryRunKey) Delete(name string) error {
	key, err := registry.OpenKey(registry.CURRENT_USER, runKeySubpath, registry.SET_VALUE)
	if errors.Is(err, registry.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer key.Close()
	if err := key.DeleteValue(name); err != nil && !errors.Is(err, registry.ErrNotExist) {
		return err
	}
	return nil
}
