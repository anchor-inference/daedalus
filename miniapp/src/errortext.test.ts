// @vitest-environment jsdom
// A failed request's toast says what failed and why, in the operator's language. The schedule card's
// toast once read only "Not Found": the stock phrase FastAPI answers for a path no route serves,
// shown as it came.
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { setLang } from "./i18n";
import { errorText } from "./ui";

function answer(status: number, body: unknown, statusText = ""): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status, statusText, headers: { "Content-Type": "application/json" } });
}

/** The error a request ends in; it must end in one. */
async function failureOf(path: string): Promise<ApiError> {
  try {
    await api.post(path);
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("the request did not fail");
}

afterEach(() => {
  vi.unstubAllGlobals();
  setLang("en");
});

describe("a failed request", () => {
  it("marks HTTP's stock phrase as no reason at all", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => answer(404, { detail: "Not Found" })));
    const failure = await failureOf("/api/recurring/s1/run");
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure.bare).toBe(true);
    expect(errorText(failure)).not.toMatch(/Not Found/);
  });

  it("keeps the host's own reason, and says what failed before it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => answer(403, { detail: "the previous scheduled run is still active" })));
    const failure = await failureOf("/api/recurring/s1/run");
    expect(failure.bare).toBe(false);
    expect(errorText(failure, "Could not run the schedule")).toBe("Could not run the schedule: the previous scheduled run is still active");
  });

  it("reads a refusal's reason out of an object detail", async () => {
    // A revision conflict answers {reason, current_revision}; its reason used to become "Request failed (409)".
    vi.stubGlobal("fetch", vi.fn(async () => answer(409, { detail: { reason: "the schedule changed", current_revision: 3 } })));
    const failure = await failureOf("/api/recurring/s1/remove");
    expect(failure.message).toBe("the schedule changed");
    expect((failure.data.detail as { current_revision: number }).current_revision).toBe(3);
  });

  it("says a bare status in Russian on the Russian page", () => {
    setLang("ru");
    const text = errorText(new ApiError(404, "Request failed (404)", {}, true), "Не удалось запустить расписание");
    expect(text).toBe("Не удалось запустить расписание: не найдено на сервере");
  });

  it("says a status it has no words for by its number", () => {
    expect(errorText(new ApiError(418, "Request failed (418)", {}, true))).toMatch(/418/);
  });
});
