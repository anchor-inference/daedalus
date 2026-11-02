package sidechan

import "strings"

// trimFinalPath turns the path GetFinalPathNameByHandle gives ("\\?\C:\x", "\\?\UNC\host\share\x")
// into the one a caller wrote ("C:\x", "\\host\share\x"), so it compares with roots and the deny
// list. It is text, so every platform's tests check it.
func trimFinalPath(p string) string {
	if rest, ok := strings.CutPrefix(p, `\\?\UNC\`); ok {
		return `\\` + rest
	}
	return strings.TrimPrefix(p, `\\?\`)
}
