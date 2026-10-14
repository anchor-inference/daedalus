import { describe, expect, it } from "vitest";
import { nameIncoming, pastedFiles } from "./attachfiles";

describe("files that reach the app", () => {
  it("gives every pasted screenshot a name of its own and leaves real names alone", () => {
    const at = new Date("2026-10-14T10:15:00Z");
    const [shot, blob, report] = nameIncoming([
      new File(["a"], "image.png", { type: "image/png" }),
      new File(["b"], "blob", { type: "image/jpeg" }),
      new File(["c"], "report.pdf", { type: "application/pdf" }),
    ], at);
    expect(shot.name).toBe("screenshot-20261014-101500.png");
    expect(blob.name).toBe("screenshot-20261014-101500.jpg");
    expect(report.name).toBe("report.pdf");
  });

  it("finds the files a paste carries, and none in a paste of text", () => {
    const file = new File(["a"], "image.png", { type: "image/png" });
    const items = [{ kind: "string", getAsFile: () => null }, { kind: "file", getAsFile: () => file }] as unknown as DataTransferItemList;
    expect(pastedFiles({ items })).toEqual([file]);
    expect(pastedFiles({ items: [] as unknown as DataTransferItemList })).toEqual([]);
    expect(pastedFiles(null)).toEqual([]);
  });
});
