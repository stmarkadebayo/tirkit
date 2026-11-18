# The web interface

Open the server's address and sign in. The first screen is the **Overview**.

## Overview

Three blocks, in this order:

1. **Can you trust your judge?** How well the LLM judge agrees with your human labels (Cohen's kappa
   and its 95% interval) and a plain verdict. When it has not been measured, the block says **not
   measured yet**, shows how many labels you have out of the 100 a firm reading needs, and opens the
   annotation queue.
2. **Experiments and the CI gate.** Your newest runs and whether the newest one *would pass* the gate:
   the same rule as `tirkit gate` (fail only if a scorer is confidently worse than the previous run on
   the same dataset version). The real gate runs in CI; this is a preview of it.
3. **Traffic, last 24 hours.** Traces, error rate, cost and worst p95 latency. **Dashboard** in the
   navigation has the full charts.

A new project shows the setup checklist first.

## Send your first trace (the checklist)

1. **Create an API key.** The secret is shown once, when you create it.
2. **Send your first trace.** Pick Python, TypeScript or OpenTelemetry and copy the snippet. It has
   your server address and public key filled in. The secret key is filled in only right after you
   create a key; otherwise paste it over `<secret key>`. The page then shows "listening" and turns
   into "Got it" with a link to the trace when one arrives. It checks every 2.5 seconds and only
   while the tab is visible.
3. Build a dataset from traces, run an experiment, label 20 items, check judge reliability. Each step
   is ticked from real data in your project and links to where you do it.

**Dismiss** hides the checklist in this browser (a link brings it back). It disappears for good when
every step is done.

## Traces

- **Filters** live in the address bar, so a view can be shared. Search, time range and status are in
  the top row; name, release, user, session and minimum latency are under **More filters**.
- The list loads 200 rows at a time and draws only the rows in view, so very long lists stay smooth.
- **Keyboard:** `j` / `k` or the arrow keys move, `Enter` opens the trace, `x` selects it for
  *Add to dataset*, `/` jumps to search. Keys are ignored while you type in a field.
- Narrow windows hide the Tokens and User columns so the others stay readable.

## Judge reliability

- **Agreement with your labels:** kappa with its interval, a verdict, and a bar showing where it falls
  between "not reliable" (under 0.4), "some agreement" (0.4 to 0.6) and "agrees well" (0.6 or more).
  These cut-offs are conventions, not guarantees.
- **Position bias:** the share of verdicts that flip when two answers swap places (under 10% is good),
  with its 95% interval.
- **Length bias:** how much more the judge's score follows answer length than your labels do (judge
  minus humans), with its 95% interval. An interval that includes 0 means no length bias was shown.
- **More numbers:** exact agreement, weighted kappa and pass/fail kappa, each with its 95% interval.
  **Earlier reports** list kappa and its interval for every rubric version.
- **Labels** counts the labels behind the result and says what to do next.
- Reports made before intervals were recorded say **no interval recorded** (run the report again to
  get one), and a figure that was not measured says **not measured yet**, never zero. **Run a judge
  against your labels** and the rubric editor are collapsed at the bottom.

## Dashboard and alerts

- **Scores** plots each score's average per period, with its 95% interval as whiskers on each point
  and in the tooltip (shown from two scores in a period up). Where whiskers overlap, the change may be noise.
- A **score average** alert fires only when the whole 95% interval of the average is past the
  threshold, so a few low scores do not page anyone. Its message gives the average, the interval and
  the number of scores. Error-rate, latency, cost and trace-count alerts compare the measured value
  directly, because they count all traffic, not a sample.

## Settings: redaction

Choose how values are redacted **before they are stored**: **Off**, **Secrets** (keys, tokens, cards,
IBANs, passwords; the default for new projects) or **Strict** (also emails, phone numbers and IP
addresses). The page shows how many values have been redacted. To keep data from leaving your process
at all, redact in the SDK too: `tirkit.init(redact="strict")`. See [Security](security.md).

## On a phone

Below 860 pixels the navigation becomes a menu button that opens a drawer (Escape or the close button
shuts it). Tables scroll inside their own box; the page itself does not scroll sideways.
