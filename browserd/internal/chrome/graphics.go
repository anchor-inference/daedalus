package chrome

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// The WebGL modes a browser starts in. Off is the browser as it always started: no switch about
// graphics at all, which in the container means no WebGL — Chromium finds only Mesa's llvmpipe on the
// virtual display, keeps llvmpipe on its GPU blocklist, and since 137 no longer falls back to
// SwiftShader for WebGL unless told to (measured on 151: "WebGL1 blocklisted", featureStatus
// webgl=disabled_off). On a native install with a working GPU, Chromium's own choice already gives
// WebGL there, and off leaves that choice alone.
const (
	WebGLOff      = "off"
	WebGLSoftware = "software"
	WebGLGPU      = "gpu"
)

// WebGLModes are the modes in the order of what they ask of the machine.
var WebGLModes = []string{WebGLOff, WebGLSoftware, WebGLGPU}

// ValidWebGL says whether mode is one of WebGLModes.
func ValidWebGL(mode string) bool {
	for _, m := range WebGLModes {
		if m == mode {
			return true
		}
	}
	return false
}

// GraphicsFlags are the switches for one WebGL mode on goos. virtual is true when the browser has no
// display of the machine's own to draw on: headless, or headed on the daemon's Xvfb.
//
// Software is ANGLE on SwiftShader, the CPU renderer every Chromium and Google Chrome ships: the same
// on every platform and in every image, with nothing to install. "Unsafe" is Chromium's word for
// letting a page's shaders run through a JIT in the GPU process; it is the reason this mode waits for
// the operator's yes. Mesa's llvmpipe measured faster on a 16-core server (12 fps against 2.4 on a
// heavy three.js scene) but exists only where Mesa does and only headed, so it was not taken.
//
// GPU lifts the blocklist so Chromium uses the machine's GPU whatever it thinks of the driver. On
// Linux without a display of its own the only way to the GPU is Vulkan on the render node: Xvfb has no
// DRI3, so OpenGL there is always llvmpipe even with /dev/dri passed in (measured: the GT 710 reached
// through ANGLE on Vulkan, Mesa's NVK, gave 34-38 fps where llvmpipe gave 12). With a real display,
// and on macOS and Windows, Chromium's own backend (OpenGL, Metal, Direct3D) is left to it.
func GraphicsFlags(mode, goos string, virtual bool) []string {
	switch mode {
	case WebGLSoftware:
		return []string{"--use-angle=swiftshader", "--enable-unsafe-swiftshader"}
	case WebGLGPU:
		if goos == "linux" && virtual {
			return []string{"--use-angle=vulkan", "--ignore-gpu-blocklist"}
		}
		return []string{"--ignore-gpu-blocklist"}
	}
	return nil
}

// GPUReady says whether gpu mode can reach a GPU here, and if not, why, in words an operator can act
// on. Only Linux without a display of its own is checked: there the browser needs a render node it may
// open and a Vulkan driver for it that is not a CPU one. A desktop session, macOS and Windows have
// the machine's GPU behind Chromium's own backend.
func GPUReady(goos string, virtual bool) (bool, string) {
	if goos != "linux" || !virtual {
		return true, ""
	}
	return gpuReadyLinux("/dev/dri", vulkanICDDirs)
}

// vulkanICDDirs are where the Vulkan loader looks for drivers' manifests on Linux.
var vulkanICDDirs = []string{"/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d"}

func gpuReadyLinux(dri string, icdDirs []string) (bool, string) {
	nodes, _ := filepath.Glob(filepath.Join(dri, "renderD*"))
	if len(nodes) == 0 {
		return false, "no GPU render node (" + dri + "/renderD*) is visible here; in a container, pass the GPU in (BROWSER_GPU_DEVICE)"
	}
	opened := false
	for _, n := range nodes {
		if f, err := os.OpenFile(n, os.O_RDWR, 0); err == nil {
			f.Close()
			opened = true
			break
		}
	}
	if !opened {
		return false, "the GPU render node " + nodes[0] + " cannot be opened by this user; give the service the node's group (BROWSER_GPU_GROUP)"
	}
	if !hardwareVulkan(icdDirs) {
		return false, "no Vulkan driver for a GPU is installed (mesa-vulkan-drivers); build the browser image with BROWSER_GPU=1"
	}
	return true, ""
}

// hardwareVulkan says whether a Vulkan driver manifest other than a CPU one (lavapipe) is installed.
func hardwareVulkan(dirs []string) bool {
	for _, dir := range dirs {
		entries, _ := os.ReadDir(dir)
		for _, e := range entries {
			if !strings.HasSuffix(e.Name(), ".json") {
				continue
			}
			data, err := os.ReadFile(filepath.Join(dir, e.Name()))
			if err != nil {
				continue
			}
			var m struct {
				ICD struct {
					LibraryPath string `json:"library_path"`
				} `json:"ICD"`
			}
			if json.Unmarshal(data, &m) != nil || m.ICD.LibraryPath == "" {
				continue
			}
			if strings.Contains(m.ICD.LibraryPath, "lvp") || strings.Contains(e.Name(), "lvp") {
				continue
			}
			return true
		}
	}
	return false
}
