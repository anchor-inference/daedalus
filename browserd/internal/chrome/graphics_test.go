package chrome

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// The switches each WebGL mode starts a browser with, on each platform. Off adds nothing, so the
// browser is exactly the one that ran before modes existed; software is SwiftShader everywhere; gpu
// reaches the GPU through Vulkan only on Linux without a display of the machine's own, where OpenGL
// would be llvmpipe whatever device is passed in.
func TestGraphicsFlagsPerModeAndPlatform(t *testing.T) {
	swiftshader := []string{"--use-angle=swiftshader", "--enable-unsafe-swiftshader"}
	for _, c := range []struct {
		mode, goos string
		virtual    bool
		want       []string
	}{
		{WebGLOff, "linux", true, nil},
		{WebGLOff, "windows", false, nil},
		{"", "linux", true, nil},
		{WebGLSoftware, "linux", true, swiftshader},
		{WebGLSoftware, "linux", false, swiftshader},
		{WebGLSoftware, "darwin", false, swiftshader},
		{WebGLSoftware, "windows", false, swiftshader},
		{WebGLGPU, "linux", true, []string{"--use-angle=vulkan", "--ignore-gpu-blocklist"}},
		{WebGLGPU, "linux", false, []string{"--ignore-gpu-blocklist"}},
		{WebGLGPU, "darwin", false, []string{"--ignore-gpu-blocklist"}},
		{WebGLGPU, "windows", false, []string{"--ignore-gpu-blocklist"}},
		{"bogus", "linux", true, nil},
	} {
		if got := GraphicsFlags(c.mode, c.goos, c.virtual); !slices.Equal(got, c.want) {
			t.Errorf("%s on %s (virtual %v): %v, want %v", c.mode, c.goos, c.virtual, got, c.want)
		}
	}
}

// Args carries the mode's switches, and off leaves the command line as it was: nothing about the GPU,
// and in particular no --disable-gpu, which a page could notice.
func TestArgsCarryTheWebGLMode(t *testing.T) {
	off := Args(Options{ProfileDir: "/p", Virtual: true})
	for _, a := range off {
		if slices.Contains([]string{"--disable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"}, a) || strings.HasPrefix(a, "--use-angle") {
			t.Fatalf("off mode has a graphics switch: %v", off)
		}
	}
	soft := Args(Options{ProfileDir: "/p", WebGL: WebGLSoftware, Virtual: true})
	if !slices.Contains(soft, "--use-angle=swiftshader") || !slices.Contains(soft, "--enable-unsafe-swiftshader") || len(soft) != len(off)+2 {
		t.Fatalf("software: %v", soft)
	}
	// The operator's own switches come after the mode's, so theirs win where both speak.
	own := Args(Options{ProfileDir: "/p", WebGL: WebGLSoftware, Args: []string{"--use-angle=gl"}})
	if slices.Index(own, "--use-angle=gl") < slices.Index(own, "--use-angle=swiftshader") {
		t.Fatalf("the operator's switch is not last: %v", own)
	}
}

func TestValidWebGL(t *testing.T) {
	for _, m := range []string{"off", "software", "gpu"} {
		if !ValidWebGL(m) {
			t.Errorf("%s refused", m)
		}
	}
	for _, m := range []string{"", "on", "GPU", "hardware"} {
		if ValidWebGL(m) {
			t.Errorf("%q taken", m)
		}
	}
}

// gpu mode on Linux without a display needs a render node this user can open and a Vulkan driver for
// a GPU; each missing piece is named with what the operator does about it.
func TestGPUReadyNamesWhatIsMissing(t *testing.T) {
	if ok, why := GPUReady("windows", true); !ok || why != "" {
		t.Fatalf("windows: %v %q", ok, why)
	}
	if ok, _ := GPUReady("linux", false); !ok {
		t.Fatal("a desktop session's GPU is Chromium's own to find")
	}
	dir := t.TempDir()
	dri, icd := filepath.Join(dir, "dri"), filepath.Join(dir, "icd.d")
	for _, d := range []string{dri, icd} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if ok, why := gpuReadyLinux(dri, []string{icd}); ok || !strings.Contains(why, "no GPU render node") {
		t.Fatalf("no node: %v %q", ok, why)
	}
	node := filepath.Join(dri, "renderD128")
	if err := os.WriteFile(node, nil, 0o000); err != nil {
		t.Fatal(err)
	}
	if os.Getuid() != 0 {
		if ok, why := gpuReadyLinux(dri, []string{icd}); ok || !strings.Contains(why, "BROWSER_GPU_GROUP") {
			t.Fatalf("a node nobody may open: %v %q", ok, why)
		}
	}
	if err := os.Chmod(node, 0o600); err != nil {
		t.Fatal(err)
	}
	if ok, why := gpuReadyLinux(dri, []string{icd}); ok || !strings.Contains(why, "mesa-vulkan-drivers") {
		t.Fatalf("no driver: %v %q", ok, why)
	}
	// Lavapipe is Vulkan on the CPU: no better than software mode, and not a GPU.
	write(t, filepath.Join(icd, "lvp_icd.x86_64.json"), `{"ICD": {"library_path": "/usr/lib/x86_64-linux-gnu/libvulkan_lvp.so"}}`)
	write(t, filepath.Join(icd, "broken.json"), `{`)
	if ok, _ := gpuReadyLinux(dri, []string{icd}); ok {
		t.Fatal("lavapipe was taken for a GPU driver")
	}
	write(t, filepath.Join(icd, "nouveau_icd.x86_64.json"), `{"ICD": {"library_path": "/usr/lib/x86_64-linux-gnu/libvulkan_nouveau.so"}}`)
	if ok, why := gpuReadyLinux(dri, []string{icd}); !ok || why != "" {
		t.Fatalf("a node and a driver: %v %q", ok, why)
	}
}

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}
