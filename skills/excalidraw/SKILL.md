---
name: excalidraw
description: Draw clear, editable Excalidraw diagrams in Daedalus (flows, architectures, decision trees) with the Diagram tools, and link the operator to the canvas.
---
# Excalidraw diagrams

The diagram is a living canvas the operator opens and edits at `/app/diagrams/<id>`, not a picture.
The tools compute positions, sizes, label wrapping and arrow bindings; you decide the content.

## Workflow

1. **Lay out first.** Anything with flow or structure (steps, components, a decision tree) goes
   through `DiagramCreate` with `nodes` and `edges`, or `DiagramLayout` on an existing diagram. Do
   not place boxes by hand with coordinates.
2. **Tweak with `DiagramEdit`:** rename a node (`{id, text}`), recolour it, add a note, connect two
   existing shapes (`{type: "arrow", from, to, label}`), delete something (`delete_ids`; its label
   and arrows go with it).
3. **Read before every edit.** `DiagramRead` gives the version and the shape ids. Pass that
   `version`; if the call is refused because the diagram changed, read again and redo the change on
   the new scene. The operator may be editing it right now.
4. **Link it.** Finish with a markdown link exactly in this form:
   `[Payment flow](/app/diagrams/0123456789abcdef0123456789abcdef)`. The id is 32 hex characters;
   the chat turns that form into an in-app link.

## Content

- **Short labels:** 2–5 words. Put detail on a second line (`"Validate order\nschema + stock"`)
  rather than a sentence.
- **At most about 15 nodes** per diagram. Split a larger system into an overview and one diagram per
  part.
- **One direction per diagram:** `right` for processes and pipelines, `down` for hierarchies and
  decision trees. Avoid edges that point backwards; for a retry or loop use a `dashed` edge with a
  label, and keep it to one or two.
- **Edge labels** only where they carry meaning: the branch of a decision (`yes` / `no`), a protocol,
  a data name.
- **Stable, readable ids** (`api`, `queue`, `worker`), so later edits can name them.

## Meaning, not decoration

Shapes:

| shape | means |
| --- | --- |
| `rectangle` | a step, component or service (the default) |
| `diamond` | a decision; label it as a question |
| `ellipse` | a start or end point |

Colours (a handful, used consistently; leave most nodes uncoloured or in one colour):

| colour | means |
| --- | --- |
| `blue` | our system, the thing being explained |
| `green` | success, output, the happy path's end |
| `yellow` | a decision or something needing attention |
| `red` | failure, risk, an error path |
| `grey` | external systems, people, third parties |
| `violet`, `teal`, `orange` | extra groups when a legend says what they mean |

Write a small free `text` element as a legend when colours carry meaning the labels do not.

## Extending an existing diagram

- `DiagramLayout` with `replace=false` places the new group beside the current content; an edge may
  point at an existing shape id to connect the two. A node id that already exists is redrawn, and the
  arrows already attached to it follow.
- `replace=true` redraws the whole canvas: use it to restructure, not to add one box.
- Moving or resizing a shape with `DiagramEdit` (`{id, x, y}`) moves its label and re-routes its
  arrows; there is no need to fix arrows by hand.
