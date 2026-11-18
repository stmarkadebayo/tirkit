import type { ReactNode } from "react";

/** An empty screen that says what is missing and puts the way to fill it right there:
 *  a button (children) and/or a ready-to-copy snippet. */
export function EmptyState({ title, children, code }: { title: string; children?: ReactNode; code?: ReactNode }) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      {children}
      {code}
    </div>
  );
}
