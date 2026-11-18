import { describe, expect, it } from "vitest";
import { LABELS_GOAL, allDone, buildChecklist, nextStep, secretNote, snippet, stepsDone, type Progress } from "./onboarding";

const empty: Progress = { hasKey: false, traceCount: 0, datasets: 0, experiments: 0, labels: 0, judgeReports: 0 };

describe("buildChecklist", () => {
  it("starts with nothing done on a new project", () => {
    const steps = buildChecklist(empty);
    expect(stepsDone(steps)).toBe(0);
    expect(nextStep(steps)?.id).toBe("key");
  });
  it("counts a key as done once one exists", () => {
    expect(buildChecklist({ ...empty, hasKey: true })[0].done).toBe(true);
  });
  it("counts the key as done when traces already arrived (a non-admin cannot list keys)", () => {
    const steps = buildChecklist({ ...empty, hasKey: null, traceCount: 3 });
    expect(steps[0].done && steps[1].done).toBe(true);
    expect(nextStep(steps)?.id).toBe("dataset");
  });
  it("shows label progress and caps it at the goal", () => {
    const s = buildChecklist({ ...empty, labels: 7 }).find((x) => x.id === "labels")!;
    expect(s.detail).toContain(`7 of ${LABELS_GOAL}`);
    expect(s.done).toBe(false);
    const over = buildChecklist({ ...empty, labels: 400 }).find((x) => x.id === "labels")!;
    expect(over.done).toBe(true);
    expect(over.detail).toContain(`${LABELS_GOAL} of ${LABELS_GOAL}`);
  });
  it("is finished only when every step is done", () => {
    const all = buildChecklist({ hasKey: true, traceCount: 1, datasets: 1, experiments: 1, labels: 20, judgeReports: 1 });
    expect(allDone(all)).toBe(true);
    expect(nextStep(all)).toBeNull();
    expect(allDone(buildChecklist({ hasKey: true, traceCount: 1, datasets: 1, experiments: 1, labels: 19, judgeReports: 1 }))).toBe(false);
  });
  it("links each step to a page", () => {
    expect(buildChecklist(empty).map((s) => s.to)).toEqual(["settings", "overview", "datasets", "experiments", "annotation", "judge"]);
  });
});

describe("snippet", () => {
  const base = { host: "http://localhost:8000", publicKey: "pk-tirkit-abc", secretKey: null };
  it("fills in the host and public key in every tab", () => {
    for (const tab of ["python", "typescript", "otel"] as const) {
      const s = snippet(tab, base);
      expect(s).toContain("TIRKIT_HOST=http://localhost:8000");
      expect(s).toContain("pk-tirkit-abc");
    }
  });
  it("never prints a secret key it was not given", () => {
    for (const tab of ["python", "typescript", "otel"] as const) {
      const s = snippet(tab, base);
      expect(s).toContain("<secret key>");
      expect(s).not.toMatch(/sk-tirkit/);
    }
  });
  it("prints the secret key just created", () => {
    expect(snippet("python", { ...base, secretKey: "sk-tirkit-new" })).toContain("TIRKIT_SECRET_KEY=sk-tirkit-new");
  });
  it("uses a placeholder when the project has no key yet", () => {
    expect(snippet("python", { ...base, publicKey: null })).toContain("<public key>");
  });
  it("points OpenTelemetry at the traces endpoint without putting the secret in the header text", () => {
    const s = snippet("otel", { ...base, secretKey: "sk-tirkit-new" });
    expect(s).toContain("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:8000/v1/traces");
    expect(s).toContain('$TIRKIT_BASIC_AUTH"');
  });
});

describe("secretNote", () => {
  it("says the secret is shown once only when it is shown", () => {
    expect(secretNote({ host: "h", publicKey: "p", secretKey: "s" })).toContain("shown once");
    expect(secretNote({ host: "h", publicKey: "p", secretKey: null })).toContain("not filled in");
  });
});
