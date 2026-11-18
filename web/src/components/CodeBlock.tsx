import { useEffect, useRef, useState } from "react";

function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy") ? resolve() : reject(new Error("copy failed"));
    } catch (e) {
      reject(e);
    } finally {
      ta.remove();
    }
  });
}

/** A block of code or shell text with a copy button. */
export function CodeBlock({ code, label = "Copy" }: { code: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await copyText(code);
      setState("copied");
    } catch {
      setState("failed");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1800);
  };
  return (
    <div className="codeblock">
      <button className="copy-btn" onClick={copy} aria-label={`${label} code to clipboard`}>
        {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : label}
      </button>
      <pre tabIndex={0} aria-label="Code">{code}</pre>
      <span className="sr-only" role="status">{state === "copied" ? "Copied to clipboard" : ""}</span>
    </div>
  );
}
