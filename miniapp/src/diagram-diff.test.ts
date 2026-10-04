import { expect, it } from "vitest";
import { diagramDiff } from "./diagram-diff";

it("reports canvas changes while ignoring Excalidraw revision bookkeeping", () => {
  const before = [{ id: "a", type: "rectangle", x: 0, version: 1, versionNonce: 12 }, { id: "b", type: "text", text: "old" }];
  const after = [{ id: "a", type: "rectangle", x: 0, version: 2, versionNonce: 23 }, { id: "c", type: "text", text: "new" }];
  expect(diagramDiff(before, after)).toEqual({ added: [{ id: "c", type: "text" }], removed: [{ id: "b", type: "text" }], changed: [] });
  expect(diagramDiff(before, [{ id: "a", type: "rectangle", x: 5 }]).changed).toEqual([{ id: "a", type: "rectangle" }]);
});
