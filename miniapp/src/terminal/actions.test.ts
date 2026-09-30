// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { endQuestion } from "./actions";
import { setLang } from "../i18n";

const idle = { busy: false } as never;
const busy = { busy: true } as never;

describe("what ending a terminal asks first", () => {
  it("ends an idle shell at once", () => {
    expect(endQuestion({ owner: { kind: "free", id: null }, live: idle, title: "bash" })).toBeNull();
  });

  it("asks when a program runs in it, naming what stops", () => {
    setLang("en");
    expect(endQuestion({ owner: { kind: "session", id: "s1" }, live: busy, title: "bash" }, "npm test")).toEqual({ title: "End the terminal?", body: "npm test stops." });
  });

  it("always asks for a staff member's terminal, busy or not, and names the member", () => {
    setLang("en");
    for (const live of [idle, busy]) {
      const question = endQuestion({ owner: { kind: "staff", id: "st-ada", label: "ada" }, live, title: "ada · roadmap" });
      expect(question?.title).toBe("End the member's session?");
      expect(question?.body).toContain("ada");
    }
    setLang("ru");
    expect(endQuestion({ owner: { kind: "staff", id: "st-ada", label: "ada" }, live: idle, title: "" })?.body).toContain("сотрудника ada");
  });
});
