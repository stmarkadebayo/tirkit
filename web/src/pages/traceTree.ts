// Builds the span tree shown on the trace page: a flat list in display order with depths.
//
// Every span appears exactly once, whatever the parent links say:
// - a span whose parent is not in the trace (an orphan) is shown as a root, flagged "orphan";
// - a span that names itself as parent is shown as a root, flagged "self-parent";
// - spans in a parent cycle (a -> b -> a) are reachable from no root; the earliest one is shown
//   as a root, flagged "cycle", and the rest of the cycle hangs under it.
// It runs in O(n log n) (sorting siblings by start), never copies arrays per span and walks with
// an explicit stack, so a 10,000-span trace or a 10,000-deep chain is fine.

export interface TreeSpan {
  span_id: string;
  parent_id: string | null;
  start: number;
}

export type TreeIssue = "orphan" | "self-parent" | "cycle";

export interface TreeNode<S extends TreeSpan> {
  span: S;
  depth: number;
  issue?: TreeIssue;
}

const byStart = (a: TreeSpan, b: TreeSpan) => a.start - b.start;

export function buildTree<S extends TreeSpan>(spans: readonly S[]): TreeNode<S>[] {
  const ids = new Set<string>();
  for (const s of spans) ids.add(s.span_id);

  const children = new Map<string, S[]>();
  const roots: S[] = [];
  const issues = new Map<S, TreeIssue>();
  for (const s of spans) {
    const p = s.parent_id;
    if (p && p !== s.span_id && ids.has(p)) {
      const list = children.get(p);
      if (list) list.push(s);
      else children.set(p, [s]);
    } else {
      roots.push(s);
      if (p === s.span_id) issues.set(s, "self-parent");
      else if (p) issues.set(s, "orphan");
    }
  }
  for (const list of children.values()) list.sort(byStart);
  roots.sort(byStart);

  const out: TreeNode<S>[] = [];
  const seen = new Set<S>();
  const walk = (root: S) => {
    const stack: [S, number][] = [[root, 0]];
    while (stack.length) {
      const [s, depth] = stack.pop()!;
      if (seen.has(s)) continue;
      seen.add(s);
      const issue = issues.get(s);
      out.push(issue ? { span: s, depth, issue } : { span: s, depth });
      const kids = children.get(s.span_id);
      if (kids) for (let i = kids.length - 1; i >= 0; i--) if (!seen.has(kids[i])) stack.push([kids[i], depth + 1]);
    }
  };
  for (const r of roots) walk(r);

  if (out.length < spans.length) {
    // What is left sits in parent cycles: break each at its earliest span.
    const rest = spans.filter((s) => !seen.has(s)).sort(byStart);
    for (const s of rest) {
      if (seen.has(s)) continue;
      issues.set(s, "cycle");
      walk(s);
    }
  }
  return out;
}
