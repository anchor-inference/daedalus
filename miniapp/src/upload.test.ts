// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { errorText } from "./ui";

describe("a failed upload", () => {
  it("names a proxy's size refusal instead of showing its status line", () => {
    // nginx refused a 150 MB video with an HTML 413 page; the operator saw only a lost connection.
    expect(errorText(new ApiError(413, "Request failed (413)"))).toMatch(/larger than the server accepts|больше, чем принимает сервер/);
  });

  it("still reads a dropped connection as no connection", () => {
    expect(errorText(new TypeError("Failed to fetch"))).not.toBe("Failed to fetch");
  });
});
