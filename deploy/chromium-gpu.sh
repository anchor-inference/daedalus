#!/bin/sh
# Stands in the image where Playwright's Chromium binaries were, each renamed to <name>.real beside it.
#
# An agent's test script launches Chromium with no switch about graphics (`p.chromium.launch()`), and
# headless Chromium then draws WebGL on SwiftShader, on the processor: a three.js page with 40 000 points
# drew at 1 to 3 frames a second while other work held the processors, and the agent concluded the
# machine had no GPU. Playwright reads no environment variable for a browser's switches and Chromium no
# flags file, so the switches are added here, where every launch passes.
#
# Only when the agent's container asks for it (DAEDALUS_CHROMIUM_GPU=1, set in deploy/compose.yaml) and a
# render node can be opened: the browser service runs the same binaries under browserd, which chooses how
# its browser draws, and a guest's sandbox has no render node. Vulkan is the only way to the GPU here:
# ANGLE on OpenGL found no display and fell back to SwiftShader, and without --ignore-gpu-blocklist the
# GT 710 on Mesa's NVK gave a page no WebGL at all. The switches go first, so a script's own
# (--use-angle=swiftshader, for one) still win: Chromium keeps the last value of a switch.
real="$(readlink -f "$0").real"
if [ "${DAEDALUS_CHROMIUM_GPU:-}" = 1 ]; then
    for node in /dev/dri/renderD*; do
        if [ -c "$node" ] && [ -r "$node" ] && [ -w "$node" ]; then
            exec "$real" --use-angle=vulkan --ignore-gpu-blocklist "$@"
        fi
    done
fi
exec "$real" "$@"
