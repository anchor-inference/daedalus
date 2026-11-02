//go:build !linux

package sessions

// procsConfirm cannot read another process's open files or folder here without a privilege the
// daemon does not ask for, so the time of the last record decides alone (known is false).
func procsConfirm(e *Env, names, files []string, cwd string) (confirmed, known bool) {
	return false, false
}
