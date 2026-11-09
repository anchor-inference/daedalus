// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { bulkLine, endQuestion, type Endable } from "./actions";
import { setLang } from "../i18n";

const shell = (fields: Partial<Endable> = {}): Endable => ({ owner: { kind: "free", id: null }, title: "bash", status: "running", at_prompt: true, running: null, ...fields });

describe("what ending a terminal asks first", () => {
  it("ends a shell the host is sure sits at its prompt at once", () => {
    expect(endQuestion(shell())).toBeNull();
  });

  it("asks when a command runs in it, naming the command", () => {
    setLang("en");
    expect(endQuestion(shell({ owner: { kind: "session", id: "s1" }, at_prompt: false, running: "npm test" }))).toEqual({ title: "End the terminal?", body: "Running: npm test" });
    setLang("ru");
    expect(endQuestion(shell({ at_prompt: false, running: "npm test" }))?.body).toBe("Выполняется: npm test");
  });

  it("asks whenever the host cannot tell, even with nothing named", () => {
    setLang("en");
    // An older host leaves the field out; a Windows shell without its marks, or an unreachable daemon, says false.
    for (const unsure of [shell({ at_prompt: undefined }), shell({ at_prompt: false })]) {
      expect(endQuestion(unsure, "build")?.body).toBe("Something may still be running in build; it stops.");
    }
  });

  it("always asks for a staff member's terminal, busy or not, and names the member and what runs", () => {
    setLang("en");
    for (const at_prompt of [true, false]) {
      const question = endQuestion(shell({ owner: { kind: "staff", id: "st-ada", label: "ada" }, at_prompt, running: "claude --resume" }));
      expect(question?.title).toBe("End the member's session?");
      expect(question?.body).toContain("ada");
      expect(question?.body).toContain("claude --resume");
    }
    setLang("ru");
    expect(endQuestion(shell({ owner: { kind: "staff", id: "st-ada", label: "ada" } }))?.body).toContain("сотрудника ada");
  });
});

describe("a question about several terminals", () => {
  it("lists every terminal, and what stops in each that is not an idle shell", () => {
    setLang("en");
    expect(bulkLine(shell())).toEqual({ title: "bash", note: "" });
    expect(bulkLine(shell({ at_prompt: false, running: "make" }))).toEqual({ title: "bash", note: "make" });
    expect(bulkLine(shell({ at_prompt: false }))).toEqual({ title: "bash", note: "may be running something" });
    expect(bulkLine(shell({ owner: { kind: "staff", id: "st", label: "ira" }, title: "" }))).toEqual({ title: "terminal", note: "ira's session" });
  });
});
