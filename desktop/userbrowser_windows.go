//go:build windows

package main

import (
	"errors"
	"os"
	"path/filepath"

	"golang.org/x/sys/windows/registry"
)

type windowsChromeRegistry struct{}

func systemChromeRegistry() chromeRegistry { return windowsChromeRegistry{} }

func hive(root regRoot) registry.Key {
	if root == regLocalMachine {
		return registry.LOCAL_MACHINE
	}
	return registry.CURRENT_USER
}

func (windowsChromeRegistry) String(root regRoot, path, name string) (string, bool) {
	key, err := registry.OpenKey(hive(root), path, registry.QUERY_VALUE)
	if err != nil {
		return "", false
	}
	defer key.Close()
	value, _, err := key.GetStringValue(name)
	return value, err == nil
}

func (windowsChromeRegistry) Values(root regRoot, path string) map[string]any {
	key, err := registry.OpenKey(hive(root), path, registry.QUERY_VALUE)
	if err != nil {
		return nil
	}
	defer key.Close()
	names, err := key.ReadValueNames(-1)
	if err != nil {
		return nil
	}
	out := make(map[string]any, len(names))
	for _, name := range names {
		if s, _, err := key.GetStringValue(name); err == nil {
			out[name] = s
			continue
		}
		if n, _, err := key.GetIntegerValue(name); err == nil {
			out[name] = n
		}
	}
	return out
}

func (windowsChromeRegistry) SetDefault(path, value string) error {
	key, _, err := registry.CreateKey(registry.CURRENT_USER, path, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	return key.SetStringValue("", value)
}

func (windowsChromeRegistry) DeleteKey(path string) error {
	if err := registry.DeleteKey(registry.CURRENT_USER, path); err != nil && !errors.Is(err, registry.ErrNotExist) {
		return err
	}
	return nil
}

// chromeRunning reports whether a Chrome holds this user data directory. A running Chrome keeps
// the file "lockfile" in it open with no sharing, and deletes it when it exits; a file that is
// there and opens is one a crash left behind.
func chromeRunning(userData string) bool {
	path := filepath.Join(userData, "lockfile")
	if !exists(path) {
		return false
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return true
	}
	file.Close()
	return false
}
