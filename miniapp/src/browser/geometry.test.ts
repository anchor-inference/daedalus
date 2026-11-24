import { describe, expect, it } from "vitest";
import { attachBox, boxToView, clampZoom, dragToWheel, fit, NO_ZOOM, pageToView, panBy, stillScale, viewToPage, visibleClip, zoomAbout, zoomed } from "./geometry";
import type { FrameMeta } from "./protocol";

const META: FrameMeta = { tab: "t1", tier: "live", w: 1280, h: 800, vw: 1280, vh: 800, scroll_x: 0, scroll_y: 1450, offset_top: 0, page_scale: 1, ts: 1 };

describe("fitting the picture", () => {
  it("letterboxes a wide page in a tall box and a tall one in a wide box", () => {
    expect(fit(640, 800, 1280, 800)).toEqual({ x: 0, y: 200, w: 640, h: 400 });
    expect(fit(1000, 400, 1280, 800)).toEqual({ x: 180, y: 0, w: 640, h: 400 });
    expect(fit(0, 400, 1280, 800)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });
});

describe("page and view", () => {
  const rect = fit(640, 500, 1280, 800);

  it("puts a page point where the frame shows it, whatever the frame's own size", () => {
    expect(pageToView(META, rect, { x: 640.5, y: 212 })).toEqual({ x: 320.25, y: 50 + 106 });
    // The same page sent at half size (a slow link): the image is 640 px, the viewport still 1280.
    const half = { ...META, w: 640, h: 400 };
    expect(pageToView(half, rect, { x: 640.5, y: 212 })).toEqual({ x: 320.25, y: 156 });
  });

  it("counts the page's zoom and the top offset", () => {
    const zoomedPage = { ...META, page_scale: 2 };
    expect(pageToView(zoomedPage, rect, { x: 100, y: 100 })).toEqual({ x: 100, y: 150 });
    const offset = { ...META, offset_top: 40 };
    expect(pageToView(offset, rect, { x: 0, y: 0 })).toEqual({ x: 0, y: 70 });
    expect(viewToPage(offset, rect, 0, 70)).toEqual({ x: 0, y: 0 });
  });

  it("maps a box corner to corner", () => {
    expect(boxToView(META, rect, { x: 600, y: 200, w: 81, h: 24 })).toEqual({ x: 300, y: 150, w: 40.5, h: 12 });
  });

  it("reads a click back into the page's pixels, and refuses the letterbox", () => {
    expect(viewToPage(META, rect, 320.25, 156)).toEqual({ x: 640.5, y: 212 });
    expect(viewToPage(META, rect, 100, 20)).toBeNull();
    expect(viewToPage(META, rect, 100, 460)).toBeNull();
  });

  it("goes there and back through a pinch", () => {
    const z = zoomed(rect, 640, 500, { scale: 2, panX: 40, panY: -20 });
    const p = pageToView(META, z, { x: 311.5, y: 90 });
    expect(viewToPage(META, z, p.x, p.y)).toEqual({ x: 311.5, y: 90 });
  });

  it("turns a finger dragged up into the page scrolling down", () => {
    expect(dragToWheel(META, rect, 0, -50)).toEqual({ dx: 0, dy: 100 });
    expect(dragToWheel(META, rect, 25, 0)).toEqual({ dx: -50, dy: 0 });
  });
});

describe("the zoom", () => {
  const rect = fit(390, 700, 1280, 800);

  it("never zooms out past the fitted picture nor in past eight times", () => {
    expect(clampZoom({ scale: 0.5, panX: 10, panY: 10 }, rect, 390, 700)).toEqual(NO_ZOOM);
    expect(clampZoom({ scale: 9, panX: 0, panY: 0 }, rect, 390, 700).scale).toBe(8);
  });

  it("keeps the point it zooms about under the pointer, and pans within the picture", () => {
    const box = fit(900, 600, 1280, 800);
    const point = { x: 700, y: 400 };
    const before = viewToPage(META, box, point.x, point.y)!;
    const z = zoomAbout(NO_ZOOM, box, 900, 600, point.x, point.y, 4);
    const after = viewToPage(META, zoomed(box, 900, 600, z), point.x, point.y)!;
    expect(z.scale).toBe(4);
    expect(after.x).toBeCloseTo(before.x, 0);
    expect(after.y).toBeCloseTo(before.y, 0);
    expect(zoomAbout(z, box, 900, 600, point.x, point.y, 1 / 4)).toEqual(NO_ZOOM);
    const moved = panBy(z, box, 900, 600, -40, 25);
    expect([moved.panX, moved.panY]).toEqual([z.panX - 40, z.panY + 25]);
  });

  it("asks for a sharp frame of what the box shows, at the screen's own pixels", () => {
    const box = fit(900, 600, 1280, 800);
    expect(visibleClip(META, box, 900, 600)).toEqual({ x: 0, y: 0, w: 1280, h: 800 });
    const z = zoomed(box, 900, 600, zoomAbout(NO_ZOOM, box, 900, 600, 450, 300, 4));
    const clip = visibleClip(META, z, 900, 600)!;
    // A quarter of the page's width and height, about its centre.
    expect(clip.x).toBeGreaterThanOrEqual(479);
    expect(clip.x).toBeLessThanOrEqual(481);
    expect(clip.w).toBeGreaterThanOrEqual(320);
    expect(clip.w).toBeLessThanOrEqual(322);
    // 900 / 1280 × 4 client pixels a CSS pixel: 2.81, in quarter steps up; twice that on a dense screen, capped at 4.
    expect(stillScale(META, z, 1)).toBe(3);
    expect(stillScale(META, z, 2)).toBe(4);
    expect(stillScale(META, box, 1)).toBe(1);
  });

  it("never pans the picture off the box's centre, nor past its edges once it is more than twice the box", () => {
    const z = clampZoom({ scale: 2, panX: 10_000, panY: -10_000 }, rect, 390, 700);
    expect(z.panX).toBeCloseTo(390 / 2);
    expect(z.panY).toBeCloseTo(-(rect.h * 2) / 2);
    const far = clampZoom({ scale: 8, panX: 10_000, panY: 10_000 }, rect, 390, 700);
    expect(far.panX).toBeCloseTo((rect.w * 8 - 390) / 2);
    expect(far.panY).toBeCloseTo((rect.h * 8 - 700) / 2);
  });

  it("keeps the point it zooms about through every step of a wheel, in a letterboxed picture too", () => {
    const box = fit(461, 611, 1280, 800);
    const point = { x: 248, y: 213 };
    const before = viewToPage(META, box, point.x, point.y)!;
    let z = NO_ZOOM;
    for (let i = 0; i < 8; i++) z = zoomAbout(z, box, 461, 611, point.x, point.y, Math.exp(0.25));
    const after = viewToPage(META, zoomed(box, 461, 611, z), point.x, point.y)!;
    expect(z.scale).toBeCloseTo(Math.exp(2));
    expect(after.x).toBeCloseTo(before.x, 0);
    expect(after.y).toBeCloseTo(before.y, 0);
  });
});

describe("the box a view asks for", () => {
  it("is the element in device pixels, capped at what the daemon sends", () => {
    expect(attachBox(700, 450, 2, false)).toEqual({ max_w: 1400, max_h: 900, dpr: 2 });
    expect(attachBox(1400, 900, 2, false)).toEqual({ max_w: 2560, max_h: 1600, dpr: 2 });
    expect(attachBox(10, 10, 1, false)).toEqual({ max_w: 64, max_h: 64, dpr: 1 });
  });

  it("is smaller and coarser when saving data", () => {
    expect(attachBox(390, 700, 3, true)).toEqual({ max_w: 640, max_h: 400, dpr: 3, quality: 45 });
  });

  it("takes more pixels at JPEG 92, never degraded, while zoomed, whatever the preset", () => {
    expect(attachBox(700, 450, 1, false, "auto", 4)).toEqual({ max_w: 2800, max_h: 1800, dpr: 1, quality: 92, adaptive: false });
    expect(attachBox(700, 450, 2, false, "smooth", 8)).toEqual({ max_w: 4096, max_h: 4096, dpr: 2, quality: 92, fps: 15, adaptive: false });
  });

  it("follows the operator's preset, which data saving does not override", () => {
    expect(attachBox(1400, 900, 2, false, "sharp")).toEqual({ max_w: 2800, max_h: 1800, dpr: 2, quality: 92, fps: 4, adaptive: false });
    expect(attachBox(1400, 900, 2, true, "sharp")).toEqual({ max_w: 2800, max_h: 1800, dpr: 2, quality: 92, fps: 4, adaptive: false });
    expect(attachBox(1400, 900, 2, false, "smooth")).toEqual({ max_w: 1280, max_h: 800, dpr: 2, quality: 50, fps: 15 });
    expect(attachBox(1000, 600, 1, false, "balanced")).toEqual({ max_w: 1000, max_h: 600, dpr: 1, quality: 75, fps: 8 });
  });
});
