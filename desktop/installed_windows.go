//go:build windows

package main

import (
	"strings"

	"golang.org/x/sys/windows/registry"
)

// noteInstalledVersion keeps Apps & features truthful after an upgrade. The installer wrote the
// version it installed into its uninstall entry; an upgrade replaces the application in place
// without the installer, and the entry would go on naming the old version for as long as the
// installation lasts. Only the per-user entry whose uninstaller is in this installation's folder
// is touched, and nothing is created: a folder unpacked by hand has no entry and gets none.
func noteInstalledVersion(root, tag string) {
	const uninstall = `Software\Microsoft\Windows\CurrentVersion\Uninstall`
	parent, err := registry.OpenKey(registry.CURRENT_USER, uninstall, registry.ENUMERATE_SUB_KEYS)
	if err != nil {
		return
	}
	defer parent.Close()
	names, err := parent.ReadSubKeyNames(-1)
	if err != nil {
		return
	}
	ours := strings.ToLower(strings.TrimRight(root, `\`) + `\Uninstall Daedalus.exe`)
	for _, name := range names {
		key, err := registry.OpenKey(parent, name, registry.QUERY_VALUE|registry.SET_VALUE)
		if err != nil {
			continue
		}
		command, _, err := key.GetStringValue("UninstallString")
		if err == nil && strings.Contains(strings.ToLower(command), ours) {
			_ = key.SetStringValue("DisplayVersion", strings.TrimPrefix(tag, releaseTagPrefix))
		}
		key.Close()
	}
}
