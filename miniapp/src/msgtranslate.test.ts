// @vitest-environment jsdom
// Whether "Translate" is worth offering under a text: only a script settles that cheaply.
import { describe, expect, it } from "vitest";
import { alreadyIn } from "./msgtranslate";

describe("alreadyIn", () => {
  it("knows a text in the operator's own script, code and paths aside", () => {
    expect(alreadyIn("Готово: `parse` теперь закрывает файл в src/app/config.py.", "ru")).toBe(true);
    expect(alreadyIn("No MR pipelines on `main`. Merging both.", "ru")).toBe(false);
    // An English note full of identifiers, with one Russian word quoted, is still English.
    expect(alreadyIn("Renamed the field to «цена» in menu.json and the loader.", "ru")).toBe(false);
  });

  it("offers every text to a language written in Latin letters, which a script cannot tell apart", () => {
    expect(alreadyIn("The loop returns early.", "en")).toBe(false);
    expect(alreadyIn("Die Schleife endet früh.", "de")).toBe(false);
  });

  it("offers a text that is only code", () => {
    expect(alreadyIn("```\nnpm test\n```", "ru")).toBe(false);
  });
});
