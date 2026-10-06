"""The agent's diagram tools and the scene builders under them, against an in-memory store."""

from __future__ import annotations

import copy
import json
import math
import uuid
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.stores import diagram_scene as scene_kit
from daedalus.stores.diagrams import validate_scene
from daedalus.tools import diagrams as diagram_tools

FLOW_NODES = [
    {"id": "start", "label": "Start", "shape": "ellipse", "color": "green"},
    {"id": "check", "label": "Is the request valid?", "shape": "diamond", "color": "yellow"},
    {"id": "work", "label": "Process the request and write the result to the database", "color": "blue"},
    {"id": "fail", "label": "Отклонить запрос", "color": "red"},
    {"id": "end", "label": "Done", "shape": "ellipse"},
]
FLOW_EDGES = [
    {"from": "start", "to": "check"},
    {"from": "check", "to": "work", "label": "yes"},
    {"from": "check", "to": "fail", "label": "no", "dashed": True},
    {"from": "work", "to": "end"},
]


class FakeStore:
    """The store contract the tools rely on, kept in memory so these tests need no database."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.sources: list[str] = []

    async def create(self, title: str, scene: dict[str, Any] | None = None, *, source: str = "agent") -> dict[str, Any]:
        diagram_id = uuid.uuid4().hex
        self.rows[diagram_id] = {
            "id": diagram_id, "title": title, "version": 1, "created_at": "now", "updated_at": "now",
            "scene": validate_scene(copy.deepcopy(scene or {"elements": [], "appState": {}, "files": {}})),
        }
        self.sources.append(source)
        return copy.deepcopy(self.rows[diagram_id])

    async def get(self, diagram_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.rows.get(diagram_id))

    async def save(self, diagram_id: str, title: str, scene: dict[str, Any], version: int, *, source: str = "agent") -> dict[str, Any]:
        row = self.rows.get(diagram_id)
        if row is None:
            raise KeyError(diagram_id)
        if row["version"] != version:
            raise RuntimeError("the diagram changed elsewhere; reload before saving")
        row.update({"title": title, "scene": validate_scene(copy.deepcopy(scene)), "version": version + 1})
        self.sources.append(source)
        return copy.deepcopy(row)

    async def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [{key: row[key] for key in ("id", "title", "version", "created_at", "updated_at")} for row in self.rows.values()]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(diagram_tools, "_store", lambda _context: fake)
    return fake


def context() -> ToolContext:
    return ToolContext(tenant_id="operator", run_id="run", session_id="session")


def assert_consistent(elements: list[dict[str, Any]]) -> None:
    """Every binding is written on both sides and every label sits inside its shape, as the editor needs."""
    validate_scene({"elements": elements, "appState": {}, "files": {}})
    by_id = {element["id"]: element for element in elements}
    for element in elements:
        listed = {item["id"] for item in element.get("boundElements") or []}
        for bound_id in listed:
            assert bound_id in by_id, f"{element['id']} lists missing {bound_id}"
        if element["type"] in scene_kit.LINEAR:
            for key in ("startBinding", "endBinding"):
                binding = element.get(key)
                if binding:
                    target = by_id[binding["elementId"]]
                    assert target["type"] in scene_kit.CONTAINERS
                    assert element["id"] in {item["id"] for item in target.get("boundElements") or []}
        if element["type"] == "text" and element.get("containerId"):
            container = by_id[element["containerId"]]
            assert element["id"] in {item["id"] for item in container.get("boundElements") or []}
            assert (element["textAlign"], element["verticalAlign"]) == ("center", "middle")
            if container["type"] in scene_kit.CONTAINERS:
                assert container["x"] <= element["x"] and element["x"] + element["width"] <= container["x"] + container["width"] + 0.01
                assert container["y"] <= element["y"] and element["y"] + element["height"] <= container["y"] + container["height"] + 0.01
                assert math.isclose(element["x"] + element["width"] / 2, container["x"] + container["width"] / 2, abs_tol=0.02)
                assert math.isclose(element["y"] + element["height"] / 2, container["y"] + container["height"] / 2, abs_tol=0.02)


def centre(element: dict[str, Any]) -> tuple[float, float]:
    return element["x"] + element["width"] / 2, element["y"] + element["height"] / 2


def arrow_ends(arrow: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    first, last = arrow["points"][0], arrow["points"][-1]
    return (arrow["x"] + first[0], arrow["y"] + first[1]), (arrow["x"] + last[0], arrow["y"] + last[1])


def test_layout_is_consistent_and_deterministic():
    first = scene_kit.layout(FLOW_NODES, FLOW_EDGES, updated=5)
    second = scene_kit.layout(copy.deepcopy(FLOW_NODES), copy.deepcopy(FLOW_EDGES), updated=5)
    assert json.dumps(first) == json.dumps(second)
    assert_consistent(first)
    by_id = {element["id"]: element for element in first}
    assert by_id["check-to-work-label"]["containerId"] == "check-to-work"
    assert by_id["check-to-fail"]["strokeStyle"] == "dashed"
    assert by_id["work"]["strokeColor"] == "#1971c2" and by_id["work"]["backgroundColor"] == "#a5d8ff"
    assert by_id["work-label"]["fontFamily"] == 5 and "\n" in by_id["work-label"]["text"]
    assert by_id["work-label"]["originalText"] == FLOW_NODES[2]["label"]


def test_edge_order_does_not_move_the_nodes():
    forward = {element["id"]: element for element in scene_kit.layout(FLOW_NODES, FLOW_EDGES)}
    backward = {element["id"]: element for element in scene_kit.layout(FLOW_NODES, list(reversed(FLOW_EDGES)))}
    assert set(forward) == set(backward)
    for node in FLOW_NODES:
        assert (forward[node["id"]]["x"], forward[node["id"]]["y"]) == (backward[node["id"]]["x"], backward[node["id"]]["y"])


@pytest.mark.parametrize("direction", ["right", "down"])
def test_layers_follow_the_edges(direction):
    elements = {element["id"]: element for element in scene_kit.layout(FLOW_NODES, FLOW_EDGES, direction)}
    axis = 0 if direction == "right" else 1
    for edge in FLOW_EDGES:
        source, target = elements[edge["from"]], elements[edge["to"]]
        assert centre(source)[axis] < centre(target)[axis]
        arrow = elements[f"{edge['from']}-to-{edge['to']}"]
        start, end = arrow_ends(arrow)
        assert start[axis] < end[axis]


def test_arrows_start_and_end_outside_their_shapes():
    elements = {element["id"]: element for element in scene_kit.layout(FLOW_NODES, FLOW_EDGES)}
    arrow = elements["start-to-check"]
    start, end = arrow_ends(arrow)
    assert start[0] > elements["start"]["x"] + elements["start"]["width"]
    assert end[0] < elements["check"]["x"] + elements["check"]["width"] / 2
    assert arrow["startBinding"] == {"elementId": "start", "focus": 0, "gap": 8, "fixedPoint": None}
    assert arrow["endArrowhead"] == "arrow" and arrow["points"][0] == [0.0, 0.0]


def test_cycles_are_laid_out_without_failing():
    nodes = [{"id": name, "label": name} for name in "abcd"]
    edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"}, {"from": "c", "to": "d"}, {"from": "d", "to": "b"}]
    elements = scene_kit.layout(nodes, edges)
    assert_consistent(elements)
    by_id = {element["id"]: element for element in elements}
    assert by_id["a"]["x"] < by_id["b"]["x"] < by_id["c"]["x"] < by_id["d"]["x"]
    assert json.dumps(elements) == json.dumps(scene_kit.layout(nodes, edges))


def test_layout_refuses_bad_graphs():
    with pytest.raises(ValueError, match="appears twice"):
        scene_kit.layout([{"id": "a"}, {"id": "a"}], [])
    with pytest.raises(ValueError, match="unknown node"):
        scene_kit.layout([{"id": "a"}], [{"from": "a", "to": "b"}])
    with pytest.raises(ValueError, match="itself"):
        scene_kit.layout([{"id": "a"}], [{"from": "a", "to": "a"}])
    with pytest.raises(ValueError, match="at most"):
        scene_kit.layout([{"id": str(n)} for n in range(201)], [])
    with pytest.raises(ValueError, match="direction"):
        scene_kit.layout([{"id": "a"}], [], "up")


def test_palette_names_and_unknown_colours():
    assert scene_kit.resolve_color("blue") == ("#1971c2", "#a5d8ff")
    assert scene_kit.resolve_color("#AbCdEf") == ("#abcdef", "transparent")
    assert scene_kit.color_name("#2f9e44") == "green"
    assert scene_kit.color_name("#123456") is None
    assert scene_kit.shape("rectangle", "box", 0, 0, color="red", fill=False)["backgroundColor"] == "transparent"
    assert scene_kit.shape("rectangle", "box", 0, 0, fill="yellow")["backgroundColor"] == "#ffec99"
    with pytest.raises(ValueError) as caught:
        scene_kit.resolve_color("magenta")
    assert "blue" in str(caught.value) and "teal" in str(caught.value)


def test_labels_fit_ellipses_and_diamonds():
    for kind in ("rectangle", "ellipse", "diamond"):
        width, height = scene_kit.fit_shape_to_label("A rather long label that has to wrap over lines", kind)
        container = scene_kit.shape(kind, f"{kind}-box", 10, 20, width, height)
        label = scene_kit.bound_label(container, "A rather long label that has to wrap over lines")
        inner_width, inner_height = scene_kit.inner_box(kind, container["width"], container["height"])
        assert label["width"] <= inner_width + 0.01 and label["height"] <= inner_height + 0.01
        assert container["height"] == height
        assert_consistent([container, label])
        assert container["boundElements"] == [{"id": f"{kind}-box-label", "type": "text"}]
        scene_kit.bound_label(container, "again")
        assert container["boundElements"] == [{"id": f"{kind}-box-label", "type": "text"}]


def test_a_short_shape_grows_to_hold_its_label():
    container = scene_kit.shape("rectangle", "box", 0, 0, 120, 30)
    label = scene_kit.bound_label(container, "one two three four five six seven eight")
    assert container["height"] > 30
    assert_consistent([container, label])


async def test_create_with_layout_links_the_diagram(store):
    result = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES, "direction": "down"})
    assert not result.is_error, result.content
    diagram_id = result.metadata["diagram_id"]
    assert f"/app/diagrams/{diagram_id}" in result.content and f"[Flow](/app/diagrams/{diagram_id})" in result.content
    assert_consistent(store.rows[diagram_id]["scene"]["elements"])
    assert store.sources == ["agent"]


async def test_create_refuses_an_unknown_colour(store):
    result = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": [{"id": "a", "color": "magenta"}]})
    assert result.is_error and "magenta" in result.content
    assert not store.rows


async def test_read_reports_labels_on_shapes_and_arrows_as_connections(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES})
    diagram_id = made.metadata["diagram_id"]
    result = await diagram_tools.diagram_read().invoke(context(), {"diagram_id": diagram_id})
    assert not result.is_error
    text = result.content
    assert f"version 1; open /app/diagrams/{diagram_id}" in text
    assert 'check diamond' in text and 'yellow "Is the request valid?"' in text
    assert 'check-to-work arrow check -> work "yes"' in text
    assert "check-to-fail arrow check -> fail dashed" in text
    assert "work-label" not in text
    assert "Отклонить запрос" in text


async def test_edit_moves_a_shape_and_its_arrows_follow(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES})
    diagram_id = made.metadata["diagram_id"]
    before = {element["id"]: element for element in store.rows[diagram_id]["scene"]["elements"]}
    result = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "elements": [{"id": "work", "x": 900, "y": 600}]})
    assert not result.is_error, result.content
    assert f"open /app/diagrams/{diagram_id}" in result.content
    after = {element["id"]: element for element in store.rows[diagram_id]["scene"]["elements"]}
    assert_consistent(list(after.values()))
    assert (after["work"]["x"], after["work"]["y"]) == (900, 600)
    assert after["work"]["width"] == before["work"]["width"] and after["work"]["backgroundColor"] == "#a5d8ff"
    assert after["work"]["version"] == before["work"]["version"] + 1
    for arrow_id, end_index in (("check-to-work", -1), ("work-to-end", 0)):
        point = arrow_ends(after[arrow_id])[0 if end_index == 0 else 1]
        work_centre = centre(after["work"])
        assert math.hypot(point[0] - work_centre[0], point[1] - work_centre[1]) < max(after["work"]["width"], after["work"]["height"])
        assert after[arrow_id]["version"] == before[arrow_id]["version"] + 1
    assert after["check-to-work-label"]["x"] != before["check-to-work-label"]["x"]
    assert after["start"] == before["start"]
    assert after["start-to-check"] == before["start-to-check"]


async def test_edit_adds_a_bound_arrow_and_relabels_a_shape(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES[:2]})
    diagram_id = made.metadata["diagram_id"]
    result = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "elements": [
        {"id": "link", "type": "arrow", "from": "start", "to": "check", "label": "begin", "color": "grey"},
        {"id": "start", "text": "Begin here"},
        {"id": "note", "type": "text", "x": 0, "y": 300, "text": "legend: green is success"},
    ]})
    assert not result.is_error, result.content
    elements = store.rows[diagram_id]["scene"]["elements"]
    assert_consistent(elements)
    by_id = {element["id"]: element for element in elements}
    assert by_id["start-label"]["originalText"] == "Begin here"
    assert [element["id"] for element in elements].index("start-label") == [element["id"] for element in elements].index("start") + 1
    assert by_id["link"]["startBinding"]["elementId"] == "start" and by_id["link"]["strokeColor"] == "#495057"
    assert by_id["link-label"]["containerId"] == "link"
    assert by_id["note"]["containerId"] is None


async def test_edit_deletes_a_shape_with_its_arrows_and_references(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES})
    diagram_id = made.metadata["diagram_id"]
    result = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "delete_ids": ["check"]})
    assert not result.is_error, result.content
    elements = store.rows[diagram_id]["scene"]["elements"]
    assert_consistent(elements)
    ids = {element["id"] for element in elements}
    assert not ids & {"check", "check-label", "start-to-check", "check-to-work", "check-to-work-label", "check-to-fail", "check-to-fail-label"}
    by_id = {element["id"]: element for element in elements}
    assert by_id["start"]["boundElements"] == [{"id": "start-label", "type": "text"}]
    assert {item["id"] for item in by_id["work"]["boundElements"]} == {"work-label", "work-to-end"}


async def test_stale_version_is_refused(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES[:1]})
    diagram_id = made.metadata["diagram_id"]
    first = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "elements": [{"id": "start", "x": 50}]})
    assert not first.is_error
    second = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "elements": [{"id": "start", "x": 90}]})
    assert second.is_error and "changed elsewhere" in second.content
    layout = await diagram_tools.diagram_layout().invoke(context(), {"diagram_id": diagram_id, "version": 1, "nodes": [{"id": "x"}]})
    assert layout.is_error and "changed elsewhere" in layout.content
    assert store.rows[diagram_id]["version"] == 2


async def test_edit_refuses_unknown_ids_and_arrow_ends(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES[:1]})
    diagram_id = made.metadata["diagram_id"]
    missing = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "delete_ids": ["ghost"]})
    assert missing.is_error and "ghost" in missing.content
    dangling = await diagram_tools.diagram_edit().invoke(context(), {"diagram_id": diagram_id, "version": 1, "elements": [{"type": "arrow", "from": "start", "to": "ghost"}]})
    assert dangling.is_error and "ghost" in dangling.content
    assert store.rows[diagram_id]["version"] == 1


async def test_layout_adds_beside_existing_content_and_replaces_known_nodes(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES})
    diagram_id = made.metadata["diagram_id"]
    existing = scene_kit.bounds(store.rows[diagram_id]["scene"]["elements"])
    result = await diagram_tools.diagram_layout().invoke(context(), {"diagram_id": diagram_id, "version": 1, "nodes": [
        {"id": "audit", "label": "Audit log", "color": "grey"}, {"id": "alert", "label": "Alert"},
    ], "edges": [{"from": "audit", "to": "alert"}, {"from": "fail", "to": "audit", "dashed": True}]})
    assert not result.is_error, result.content
    assert f"/app/diagrams/{diagram_id}" in result.content and "version 2" in result.content
    elements = store.rows[diagram_id]["scene"]["elements"]
    assert_consistent(elements)
    by_id = {element["id"]: element for element in elements}
    assert by_id["audit"]["x"] > existing[2]
    assert by_id["fail-to-audit"]["startBinding"]["elementId"] == "fail"

    again = await diagram_tools.diagram_layout().invoke(context(), {"diagram_id": diagram_id, "version": 2, "nodes": [
        {"id": "work", "label": "Worker", "color": "teal"}, {"id": "audit", "label": "Audit log", "color": "grey"},
    ], "edges": [{"from": "work", "to": "audit"}], "origin_x": 0, "origin_y": 900})
    assert not again.is_error, again.content
    elements = store.rows[diagram_id]["scene"]["elements"]
    assert_consistent(elements)
    ids = [element["id"] for element in elements]
    assert ids.count("work") == 1 and ids.count("work-label") == 1
    by_id = {element["id"]: element for element in elements}
    assert by_id["work"]["y"] >= 900 and by_id["work-label"]["originalText"] == "Worker"
    # Arrows drawn earlier to the replaced node stay bound and follow it to its new place.
    assert by_id["check-to-work"]["endBinding"]["elementId"] == "work"
    assert {item["id"] for item in by_id["work"]["boundElements"]} >= {"check-to-work", "work-to-end", "work-to-audit", "work-label"}
    assert arrow_ends(by_id["check-to-work"])[1][1] > 800


async def test_layout_replace_redraws_the_canvas(store):
    made = await diagram_tools.diagram_create().invoke(context(), {"title": "Flow", "nodes": FLOW_NODES, "edges": FLOW_EDGES})
    diagram_id = made.metadata["diagram_id"]
    result = await diagram_tools.diagram_layout().invoke(context(), {"diagram_id": diagram_id, "version": 1, "replace": True, "nodes": [{"id": "only", "label": "Only"}]})
    assert not result.is_error, result.content
    assert [element["id"] for element in store.rows[diagram_id]["scene"]["elements"]] == ["only", "only-label"]
    assert store.sources == ["agent", "agent"]


async def test_list_returns_the_diagrams(store):
    await diagram_tools.diagram_create().invoke(context(), {"title": "Flow"})
    result = await diagram_tools.diagram_list().invoke(context(), {})
    assert not result.is_error and json.loads(result.content)[0]["title"] == "Flow"
