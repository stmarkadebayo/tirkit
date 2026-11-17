import { describe, expect, it } from "vitest";
import { REDACT_MODES, effectiveMode, redactionCounterText } from "./redaction";

describe("effectiveMode", () => {
  it("uses the mode when the server sends one", () => {
    expect(effectiveMode({ redact_mode: "secrets" })).toBe("secrets");
    expect(effectiveMode({ redact_mode: "strict", redact: false })).toBe("strict");
  });
  it("falls back to the old on/off flag, like the server", () => {
    expect(effectiveMode({ redact: true })).toBe("strict");
    expect(effectiveMode({ redact: false })).toBe("off");
    expect(effectiveMode({})).toBe("off");
    expect(effectiveMode(undefined)).toBe("off");
  });
  it("ignores an unknown mode", () => {
    expect(effectiveMode({ redact_mode: "everything" })).toBe("off");
  });
});

describe("redactionCounterText", () => {
  it("says so when redaction is off and nothing was redacted", () => {
    expect(redactionCounterText(0, "off")).toContain("off");
  });
  it("does not claim redaction ran when none did", () => {
    expect(redactionCounterText(null, "secrets")).toContain("No values redacted yet");
  });
  it("pluralises", () => {
    expect(redactionCounterText(1, "strict")).toContain("1 value has");
    expect(redactionCounterText(1200, "strict")).toMatch(/1.200 values have/);
  });
});

describe("REDACT_MODES", () => {
  it("lists every mode once, strictest last", () => {
    expect(REDACT_MODES.map((m) => m.mode)).toEqual(["off", "secrets", "strict"]);
  });
});
