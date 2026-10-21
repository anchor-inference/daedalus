// The scope picker's decisions on their own: the scope each tab makes, the minutes a grant may last,
// where a picture is read from, and how a monitor is named, sized and ordered.
import { describe, expect, it } from "vitest";
import { setLang } from "./i18n";
import {
  appFace,
  buildScope,
  chosenPreview,
  displayName,
  displayResolution,
  displayScale,
  previewUrl,
  sortedDisplays,
  stillThere,
  validMinutes,
  windowCards,
  type ComputerApp,
  type ComputerDisplay,
} from "./computerscope";

const APPS: ComputerApp[] = [
  { id: "editor", name: "Editor", reachable: true, windows: [{ id: "10", title: "notes.txt", minimized: true }, { id: "11", title: "", focused: true }] },
  { id: "setup", name: "setup", reachable: false, windows: [{ id: "13", title: "Administrator: setup" }] },
];
const BUILT_IN: ComputerDisplay = { id: "1", name: "Built-in", bounds: { x: 0, y: 0, w: 1512, h: 982 }, primary: true, pixels_per_unit: 2, scale: 2 };
const SIDE: ComputerDisplay = { id: "2", name: " ", bounds: { x: -1920, y: 0, w: 1920, h: 1080 }, pixels_per_unit: 1, scale: 1 };

describe("the scope a choice makes", () => {
  it("names its target, and the whole desktop none", () => {
    expect(buildScope("window", "10")).toEqual({ kind: "window", window: "10" });
    expect(buildScope("app", "editor")).toEqual({ kind: "app", app: "editor" });
    expect(buildScope("display", "2")).toEqual({ kind: "display", display: "2" });
    expect(buildScope("desktop", "")).toEqual({ kind: "desktop" });
    expect(buildScope("desktop", "10")).toEqual({ kind: "desktop" });
    expect(buildScope("window", "")).toBeNull();
    expect(buildScope("display", "")).toBeNull();
  });

  it("holds a grant between one minute and the host's bound, in whole minutes", () => {
    expect(validMinutes(1)).toBe(true);
    expect(validMinutes(240)).toBe(true);
    expect(validMinutes(0)).toBe(false);
    expect(validMinutes(241)).toBe(false);
    expect(validMinutes(2.5)).toBe(false);
    expect(validMinutes(Number.NaN)).toBe(false);
  });

  it("lets go of a choice that is no longer on the screen, or no longer reachable", () => {
    expect(stillThere("window", "11", APPS, [])).toBe(true);
    expect(stillThere("window", "99", APPS, [])).toBe(false);
    expect(stillThere("window", "13", APPS, [])).toBe(false);
    expect(stillThere("app", "setup", APPS, [])).toBe(false);
    expect(stillThere("display", "2", APPS, [BUILT_IN])).toBe(false);
    expect(stillThere("desktop", "", APPS, [])).toBe(true);
  });
});

describe("the pictures", () => {
  it("reads a window or a display at a bounded size", () => {
    expect(previewUrl({ window: "10" })).toBe("/api/computer/preview?window=10&max=320");
    expect(previewUrl({ display: "2" }, 720)).toBe("/api/computer/preview?display=2&max=720");
    expect(previewUrl({ window: "a b&c" }, 5000)).toBe("/api/computer/preview?window=a+b%26c&max=960");
  });

  it("shows an app by its focused window, and the large picture by what was chosen", () => {
    expect(appFace(APPS[0])?.id).toBe("11");
    expect(appFace({ id: "x", name: "x", windows: [{ id: "1", title: "a", minimized: true }, { id: "2", title: "b" }] })?.id).toBe("2");
    expect(appFace({ id: "x", name: "x", windows: [] })).toBeNull();
    expect(chosenPreview("app", "editor", APPS)).toEqual({ window: "11" });
    expect(chosenPreview("display", "2", APPS)).toEqual({ display: "2" });
    expect(chosenPreview("window", "", APPS)).toBeNull();
    expect(chosenPreview("desktop", "", APPS)).toBeNull();
  });

  it("makes a card per window, saying why an elevated one cannot be chosen", () => {
    setLang("en");
    const cards = windowCards(APPS);
    expect(cards.map((c) => [c.id, c.title, c.minimized, c.blocked !== ""])).toEqual([["10", "notes.txt", true, false], ["11", "Untitled window", false, false], ["13", "Administrator: setup", false, true]]);
  });
});

describe("the monitors", () => {
  it("are named, sized in captured pixels and scaled as the system says", () => {
    setLang("en");
    expect(displayName(BUILT_IN, 0)).toBe("Built-in");
    expect(displayName(SIDE, 1)).toBe("Display 2");
    expect(displayResolution(BUILT_IN)).toBe("3024 × 1964");
    expect(displayResolution({ ...SIDE, pixels_per_unit: undefined })).toBe("1920 × 1080");
    expect(displayScale(BUILT_IN)).toBe("200 %");
    expect(displayScale(SIDE)).toBe("");
    setLang("ru");
    expect(displayName(SIDE, 1)).toBe("Монитор 2");
    setLang("en");
  });

  it("put the primary first, then the rest from the left", () => {
    const third: ComputerDisplay = { id: "3", bounds: { x: 1512, y: 0, w: 1000, h: 800 } };
    expect(sortedDisplays([third, SIDE, BUILT_IN]).map((d) => d.id)).toEqual(["1", "2", "3"]);
  });
});
