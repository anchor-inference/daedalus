package sandbox

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

// ReadSource names an object below a caller-authorized open directory and its mount destination.
// Pinning preserves the object identity, not its mutable bytes or a directory's descendants.
type ReadSource struct {
	Relative    string
	Destination string
}

// Close releases a plan's owned source descriptors. It is safe after either successful or failed
// spawn and on repeated calls; children already started hold their own descriptor references.
func (p *Plan) Close() error {
	var errs []error
	for _, file := range p.ExtraFiles {
		if err := file.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// WrapPinnedReadOnly overlays pinned objects on the ordinary write sandbox. It does not establish
// credential isolation: the caller must authorize the root and object contents independently.
// The root is borrowed during this call and must not be closed concurrently.
// Pass ExtraFiles unchanged to the spawn operation and close the plan after spawn returns.
func WrapPinnedReadOnly(o Options, root *os.File, sources []ReadSource) (Plan, error) {
	if root == nil || len(sources) == 0 || len(sources) > MaxWritable {
		return Plan{}, errors.New("sandbox: pinned sources need an open root and 1..256 objects")
	}
	if st, err := root.Stat(); err != nil || !st.IsDir() {
		return Plan{}, errors.New("sandbox: the pinned source root must be an open directory")
	}
	seen := map[string]bool{}
	for _, source := range sources {
		if source.Relative == "." || filepath.IsAbs(source.Relative) || !filepath.IsLocal(source.Relative) || filepath.Clean(source.Relative) != source.Relative {
			return Plan{}, fmt.Errorf("sandbox: source must be a clean relative path below its root: %q", source.Relative)
		}
		destination := source.Destination
		if !filepath.IsAbs(destination) || filepath.Clean(destination) != destination || destination == "/" || seen[destination] {
			return Plan{}, fmt.Errorf("sandbox: destination must be a unique clean absolute path below root: %q", destination)
		}
		seen[destination] = true
	}
	files, err := pinReadSources(root, sources)
	if err != nil {
		return Plan{}, err
	}
	owned := Plan{ExtraFiles: files}
	plan, err := Wrap(o)
	if err != nil {
		return Plan{}, errors.Join(err, owned.Close())
	}
	// exec.Cmd.ExtraFiles maps the first file to fd 3. Numeric host descriptors are never argv.
	var mounts []string
	for index, source := range sources {
		mounts = append(mounts, "--ro-bind-fd", strconv.Itoa(index+3), source.Destination)
	}
	at := len(plan.Argv) - len(o.Argv) - 3
	argv := append([]string{}, plan.Argv[:at]...)
	argv = append(argv, mounts...)
	plan.Argv = append(argv, plan.Argv[at:]...)
	plan.ExtraFiles = files
	return plan, nil
}
