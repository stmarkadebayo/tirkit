// Plain-language description of the three redaction modes. The server owns the behaviour
// (src/tirkit/redact.py); this is only the wording the Settings page shows.

export type RedactMode = "off" | "secrets" | "strict";

export interface ModeInfo {
  mode: RedactMode;
  title: string;
  summary: string;
  catches: string;
}

export const REDACT_MODES: ModeInfo[] = [
  {
    mode: "off",
    title: "Off",
    summary: "Nothing is changed. Prompts, outputs and attributes are stored as they arrive.",
    catches: "Use only when the data is already clean or you redact in your own code.",
  },
  {
    mode: "secrets",
    title: "Secrets",
    summary: "Removes credentials before storing. Default for new projects.",
    catches: "API keys, tokens, JWTs, card numbers, IBANs and password assignments.",
  },
  {
    mode: "strict",
    title: "Strict",
    summary: "Secrets plus personal contact details. Recommended for production traffic.",
    catches: "Everything in Secrets, plus email addresses, phone numbers and IP addresses.",
  },
];

export function isRedactMode(v: unknown): v is RedactMode {
  return v === "off" || v === "secrets" || v === "strict";
}

/** The mode to show for a project's settings; mirrors the server (a missing mode means the old on/off flag). */
export function effectiveMode(settings: { redact_mode?: unknown; redact?: unknown } | null | undefined): RedactMode {
  if (isRedactMode(settings?.redact_mode)) return settings.redact_mode;
  return settings?.redact ? "strict" : "off";
}

/** One sentence for the counter under the mode control. */
export function redactionCounterText(count: number | null | undefined, mode: RedactMode): string {
  const n = count ?? 0;
  if (n === 0) {
    return mode === "off"
      ? "No values have been redacted. Redaction is off."
      : "No values redacted yet. The counter goes up as traces arrive.";
  }
  return `${n.toLocaleString()} ${n === 1 ? "value has" : "values have"} been redacted so far (server and SDK).`;
}
