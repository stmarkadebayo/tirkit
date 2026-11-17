import { forwardRef, useEffect, useState } from "react";

/** A text input that commits to its owner after a short pause or on Enter, and follows the owner's value
 *  when it changes from outside (for example "Clear filters"). */
export const TextFilter = forwardRef<
  HTMLInputElement,
  { value: string; onCommit: (v: string) => void; placeholder: string; label: string; width?: number | string; type?: string }
>(function TextFilter({ value, onCommit, placeholder, label, width, type = "search" }, ref) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  useEffect(() => {
    if (text === value) return;
    const t = setTimeout(() => onCommit(text), 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  return (
    <input
      ref={ref}
      type={type}
      placeholder={placeholder}
      aria-label={label}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") onCommit(text);
      }}
      style={{ width, flex: width ? undefined : "1 1 220px" }}
    />
  );
});
