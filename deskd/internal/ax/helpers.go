package ax

import (
	"path/filepath"
	"strings"
)

// The tool on macOS answers through a helper process of its own (its "command host"), which it
// starts in a process group of its own and which keeps the system's element handles for up to 15
// idle minutes. Killing a call's process group does not reach it, and left alone it would outlive
// the lease that started it, holding handles to the windows of a grant that has ended. The daemon
// finds it by what it inherited from the call: the tool's internal host marker and the lease's state
// root, which only the daemon sets, so no other program's copy of the tool is ever touched.
//
// A process still inside its exec shows an empty environment and cannot be recognised. That never
// hides a real helper: the search runs holding the tool's lock, so no call is starting one, and the
// call that started one waited until the helper answered on its socket, long past its exec.

const helperMarker = "AGENT_DESKTOP_INTERNAL_COMMAND_HOST=1"

// isHelperOf reports whether a process environment is a helper started under root.
func isHelperOf(env []string, root string) bool {
	marked, inside := false, false
	for _, kv := range env {
		if kv == helperMarker {
			marked = true
		}
		if home, ok := strings.CutPrefix(kv, "AGENT_DESKTOP_HOME="); ok {
			inside = home == root || strings.HasPrefix(home, root+string(filepath.Separator))
		}
	}
	return marked && inside
}

// stopHelpers kills every helper started under root.
func stopHelpers(root string) {
	if root == "" {
		return
	}
	for pid, env := range userProcessEnvs() {
		if isHelperOf(env, root) {
			killProcess(pid)
		}
	}
}
