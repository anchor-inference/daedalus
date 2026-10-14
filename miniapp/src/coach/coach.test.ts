import { afterEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { COMPOSER_CLEARANCE, EDGE, clampPosition, defaultPosition, readPosition, seedMessage, windowSize, writePosition } from "./coach";

afterEach(() => setLang("en"));

describe("the coach's first turn", () => {
  it("frames the draft in the reader's language, so the coach answers in it", () => {
    setLang("en");
    expect(seedMessage("  fix the menu page \n")).toBe("Help me improve this prompt:\n\nfix the menu page");
    setLang("ru");
    expect(seedMessage("почини меню")).toBe("Помоги улучшить этот промпт:\n\nпочини меню");
  });

  it("asks for help writing one when there is no draft", () => {
    setLang("en");
    expect(seedMessage("   ")).toBe("Help me write a prompt.");
    setLang("ru");
    expect(seedMessage("")).toBe("Помоги мне написать промпт.");
  });

  it("keeps a draft's own braces and bars as they were typed", () => {
    expect(seedMessage("use {name} | {n}")).toContain("use {name} | {n}");
  });
});

describe("where the window stands", () => {
  const screen = { width: 1440, height: 900 };

  it("is sized for a few turns and never larger than the screen", () => {
    expect(windowSize(screen)).toEqual({ width: 460, height: 620 });
    const small = windowSize({ width: 400, height: 500 });
    expect(small.width).toBe(400 - 2 * EDGE);
    expect(small.height).toBe(500 - EDGE - COMPOSER_CLEARANCE);
  });

  it("starts on the right, above the composer and inside the screen", () => {
    const size = windowSize(screen);
    const at = defaultPosition(size, screen);
    expect(at.x + size.width).toBeLessThanOrEqual(screen.width - EDGE);
    expect(at.y + size.height).toBeLessThanOrEqual(screen.height - COMPOSER_CLEARANCE);
    expect(at.x).toBeGreaterThan(screen.width / 2);
    expect(at.y + size.height).toBe(screen.height - COMPOSER_CLEARANCE);
  });

  it("is pulled back inside when dragged off or remembered from a larger screen", () => {
    const size = { width: 460, height: 620 };
    expect(clampPosition({ x: -300, y: -50 }, size, screen)).toEqual({ x: EDGE, y: EDGE });
    expect(clampPosition({ x: 5000, y: 5000 }, size, screen)).toEqual({ x: 1440 - 460 - EDGE, y: 900 - 620 - EDGE });
    // A screen smaller than the window keeps the title bar on it.
    expect(clampPosition({ x: 200, y: 200 }, size, { width: 300, height: 300 })).toEqual({ x: EDGE, y: EDGE });
  });

  it("is remembered per viewer, and junk or blocked storage is no position", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => void kept.set(key, value) };
    vi.stubGlobal("window", { localStorage: storage });
    writePosition({ x: 120, y: 80 });
    expect(readPosition()).toEqual({ x: 120, y: 80 });
    kept.set("daedalus.coach.position", "{\"x\":\"left\"}");
    expect(readPosition()).toBeNull();
    kept.set("daedalus.coach.position", "not json");
    expect(readPosition()).toBeNull();
    // A private window or blocked site data: the accessor itself throws.
    vi.stubGlobal("window", { get localStorage(): Storage { throw new Error("blocked"); } });
    expect(readPosition()).toBeNull();
    expect(() => writePosition({ x: 1, y: 2 })).not.toThrow();
    vi.unstubAllGlobals();
  });
});
