import { describe, expect, it } from "vitest";
import { folderNameProblem, freeName, slugify, SLUG_MAX } from "./slug";

describe("a new project's folder name", () => {
  it("spells a Russian name in Latin letters", () => {
    expect(slugify("Умный дом")).toBe("umnyj-dom");
    expect(slugify("Прошивка ESP32")).toBe("proshivka-esp32");
    expect(slugify("Щёки и Шея")).toBe("shcheki-i-sheya");
  });
  it("keeps Latin names readable and drops what a path should not hold", () => {
    expect(slugify("DevHub")).toBe("devhub");
    expect(slugify("  Café / Bakery: 2026!  ")).toBe("cafe-bakery-2026");
    expect(slugify("???")).toBe("project");
  });
  it("stops at a readable length without a trailing hyphen", () => {
    const long = slugify("очень ".repeat(30));
    expect(long.length).toBeLessThanOrEqual(SLUG_MAX);
    expect(long.endsWith("-")).toBe(false);
  });
  it("refuses a name the server would refuse", () => {
    for (const bad of ["", " ", ".git", "a/b", "a\\b", "c:", "x".repeat(81)]) expect(folderNameProblem(bad), bad).toBe(true);
    expect(folderNameProblem("umnyj-dom")).toBe(false);
  });
  it("finds the next free name", () => {
    const taken = new Set(["devhub", "devhub-2"]);
    expect(freeName("devhub", (name) => taken.has(name))).toBe("devhub-3");
    expect(freeName("site", (name) => taken.has(name))).toBe("site");
  });
});
