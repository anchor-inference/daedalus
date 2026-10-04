---
name: excalidraw
description: Create editable diagrams in Daedalus with native Excalidraw tools and link the operator to the canvas.
---
# Excalidraw diagrams

- Use `DiagramCreate` for a new canvas, `DiagramRead` to inspect its elements and version, and `DiagramEdit` to add or revise shapes. The result's `/app/diagrams/<id>` address opens the native editor.
- Sketch with rectangles, diamonds, ellipses, text and arrows. Keep labels as separate text elements so the operator can edit them. Give each shape a stable id when changing it later.
- Lay out shapes with enough space for labels and arrows. An arrow's `x,y` is its start; `points` are offsets from there, such as `[[0,0],[160,0]]`.
- Pass the version from the latest `DiagramRead` to `DiagramEdit`. If another edit won, read again and apply the intended changes to that scene.
- The diagram is an editable scene, not a screenshot. Use these tools for persistent work; a temporary room or CLI session is unnecessary for an in-app canvas.
