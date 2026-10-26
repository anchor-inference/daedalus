// Private chats: the create call carries the field only when private, and a chat reads as private only
// when the host said so with a boolean, on a row's metadata or on a detail, never from a stray value.
import { describe, expect, it } from "vitest";
import { isPrivate, privateBody } from "./privatechat";

describe("private chats", () => {
  it("sends the field only for a private chat", () => {
    expect(privateBody(true)).toEqual({ private: true });
    expect(privateBody(false)).toEqual({});
  });

  it("reads a row's metadata and a detail's field, and nothing else", () => {
    expect(isPrivate({ metadata: { private: true } })).toBe(true);
    expect(isPrivate({ private: true })).toBe(true);
    expect(isPrivate({ private: false, metadata: {} })).toBe(false);
    expect(isPrivate({ metadata: { private: "yes" } })).toBe(false);
    expect(isPrivate({ metadata: { private: 1 } })).toBe(false);
    expect(isPrivate(null)).toBe(false);
    expect(isPrivate(undefined)).toBe(false);
  });
});
