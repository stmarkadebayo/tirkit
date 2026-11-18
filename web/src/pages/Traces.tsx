import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, projectPath, qs, type Filters, type Trace } from "../api";
import { RANGES, usePid, useRole } from "../hooks";
import { fmtAgo, fmtCost, fmtMs, fmtNum, preview } from "../format";
import { AddToDataset } from "../components/AddToDataset";
import { EmptyState } from "../components/EmptyState";
import { TraceSnippetPanel } from "../components/SetupChecklist";
import { TextFilter } from "../components/TextFilter";
import { MORE_LABELS, activeMoreCount, clearFilters, hasAnyFilter, range as rangeOf, traceQuery, withParam, type FilterKey } from "../traceFilters";
import { moveCursor, scrollToRow, visibleRange } from "../windowing";

const ROW_H = 44;
const HEAD_H = 36;
const BATCH = 200;
const COLS = "32px 84px minmax(130px, 1fr) minmax(180px, 2fr) minmax(180px, 2fr) 84px 72px 88px 110px";

function isTyping(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

export default function Traces() {
  const pid = usePid();
  const role = useRole();
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showMore, setShowMore] = useState(() => activeMoreCount(params) > 0);
  const [cursor, setCursor] = useState<number | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(480);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const get = (k: FilterKey) => params.get(k) || "";
  const set = (k: FilterKey, v: string) => setParams(withParam(params, k, v), { replace: true });
  const range = rangeOf(params);
  const [now] = useState(() => Math.floor(Date.now() / 1000));
  const filters = useQuery({ queryKey: ["filters", pid], queryFn: () => api<Filters>(projectPath(pid, "/filters")) });
  const query = useMemo(() => traceQuery(params, now), [params, now]);
  const traces = useInfiniteQuery({
    queryKey: ["traces", pid, query],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api<{ items: Trace[]; total: number }>(projectPath(pid, `/traces${qs({ ...query, limit: BATCH, offset: pageParam })}`)),
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total && last.items.length > 0 ? loaded : undefined;
    },
    placeholderData: keepPreviousData,
  });
  const rows = useMemo(() => traces.data?.pages.flatMap((p) => p.items) ?? [], [traces.data]);
  const total = traces.data?.pages[0]?.total ?? 0;
  const win = visibleRange({ scrollTop, viewportHeight: viewport - HEAD_H, rowHeight: ROW_H, count: rows.length });

  // Load the next batch when the window gets near the end of what we have.
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = traces;
  useEffect(() => {
    if (win.end >= rows.length - 30 && hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [win.end, rows.length, hasNextPage, isFetchingNextPage, fetchNextPage]);

  // Track the scroll container's height.
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewport(el.clientHeight));
    ro.observe(el);
    setViewport(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  // A new set of filters starts at the top.
  const queryKey = JSON.stringify(query);
  useEffect(() => {
    setCursor(null);
    if (listRef.current) listRef.current.scrollTop = 0;
    setScrollTop(0);
  }, [queryKey]);

  const raf = useRef(0);
  const onScroll = () => {
    if (raf.current) return;
    raf.current = requestAnimationFrame(() => {
      raf.current = 0;
      if (listRef.current) setScrollTop(listRef.current.scrollTop);
    });
  };

  const open = useCallback((i: number) => rows[i] && nav(`/p/${pid}/traces/${rows[i].trace_id}`), [rows, nav, pid]);
  const toggle = useCallback((id: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const moveTo = useCallback(
    (delta: number) => {
      setCursor((cur) => {
        const next = moveCursor(cur, delta, rows.length);
        const el = listRef.current;
        if (next !== null && el) {
          const top = scrollToRow(next, el.scrollTop, el.clientHeight, ROW_H, HEAD_H);
          if (top !== el.scrollTop) {
            el.scrollTop = top;
            setScrollTop(top);
          }
        }
        return next;
      });
    },
    [rows.length],
  );

  // Keyboard: j/k or arrows move, Enter opens, x selects, / searches. Ignored while typing in a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
      if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        moveTo(1);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        moveTo(-1);
      } else if (e.key === "PageDown") {
        e.preventDefault();
        moveTo(Math.max(1, Math.floor(viewport / ROW_H) - 1));
      } else if (e.key === "PageUp") {
        e.preventDefault();
        moveTo(-Math.max(1, Math.floor(viewport / ROW_H) - 1));
      } else if (e.key === "Enter" && cursor !== null && (e.target === document.body || e.target === listRef.current)) {
        e.preventDefault();
        open(cursor);
      } else if (e.key === "x" && cursor !== null && rows[cursor]) {
        toggle(rows[cursor].trace_id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moveTo, open, toggle, cursor, rows, viewport]);

  const moreCount = activeMoreCount(params);
  const filtered = hasAnyFilter(params);
  const loadedAll = rows.length >= total;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Traces</h1>
          <div className="muted small" aria-live="polite">
            {fmtNum(total)} traces match{rows.length < total ? `, ${fmtNum(rows.length)} loaded` : ""}
          </div>
        </div>
        <div className="row">
          {selected.size > 0 && role !== "viewer" && <AddToDataset traceIds={[...selected]} onDone={() => setSelected(new Set())} />}
          <a className="btn" href={projectPath(pid, `/export/traces.jsonl${qs({ ...query, limit: undefined, offset: undefined })}`)}>
            Export JSONL
          </a>
        </div>
      </div>
      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="row">
          <TextFilter ref={searchRef} value={get("q")} onCommit={(v) => set("q", v)} placeholder="Search inputs and outputs" label="Search inputs and outputs" />
          <select value={range} onChange={(e) => set("range", e.target.value)} aria-label="Time range">
            {Object.keys(RANGES).map((r) => <option key={r} value={r}>last {r}</option>)}
            <option value="all">all time</option>
          </select>
          <select value={get("error")} onChange={(e) => set("error", e.target.value)} aria-label="Status">
            <option value="">Any status</option>
            <option value="true">Errors only</option>
            <option value="false">OK only</option>
          </select>
          <button aria-expanded={showMore} aria-controls="more-filters" onClick={() => setShowMore(!showMore)}>
            More filters{moreCount > 0 && <span className="badge accent">{moreCount}</span>}
          </button>
          {filtered && <button onClick={() => setParams(clearFilters(params), { replace: true })}>Clear</button>}
        </div>
        {showMore && (
          <div className="row more-filters" id="more-filters">
            <select value={get("name")} onChange={(e) => set("name", e.target.value)} aria-label={MORE_LABELS.name}>
              <option value="">All names</option>
              {filters.data?.names.map((n) => <option key={n}>{n}</option>)}
            </select>
            <select value={get("release")} onChange={(e) => set("release", e.target.value)} aria-label={MORE_LABELS.release}>
              <option value="">All releases</option>
              {filters.data?.releases.map((n) => <option key={n}>{n}</option>)}
            </select>
            <TextFilter value={get("user_id")} onCommit={(v) => set("user_id", v)} placeholder="User id" label="User id" width={130} type="text" />
            <TextFilter value={get("session_id")} onCommit={(v) => set("session_id", v)} placeholder="Session id" label="Session id" width={140} type="text" />
            <select value={get("min_ms")} onChange={(e) => set("min_ms", e.target.value)} aria-label="Minimum latency">
              <option value="">Any latency</option>
              <option value="1000">≥ 1 s</option>
              <option value="5000">≥ 5 s</option>
              <option value="20000">≥ 20 s</option>
            </select>
          </div>
        )}
      </div>
      <div className="panel vtable-panel">
        <div
          ref={listRef}
          className="vtable"
          role="grid"
          aria-label="Traces"
          aria-rowcount={total + 1}
          aria-activedescendant={cursor !== null ? `trace-row-${cursor}` : undefined}
          tabIndex={0}
          onScroll={onScroll}
        >
          <div className="vtable-inner">
            <div className="vrow vhead" role="row" style={{ gridTemplateColumns: COLS, height: HEAD_H }}>
              <div role="columnheader" aria-label="Select" />
              <div role="columnheader" className="vcell">Time</div>
              <div role="columnheader" className="vcell">Name</div>
              <div role="columnheader" className="vcell">Input</div>
              <div role="columnheader" className="vcell">Output</div>
              <div role="columnheader" className="vcell num">Latency</div>
              <div role="columnheader" className="vcell num">Tokens</div>
              <div role="columnheader" className="vcell num">Cost</div>
              <div role="columnheader" className="vcell">User</div>
            </div>
            <div style={{ height: win.totalHeight, position: "relative" }}>
              <div style={{ position: "absolute", top: 0, left: 0, right: 0, transform: `translateY(${win.offsetTop}px)` }}>
                {rows.slice(win.start, win.end).map((t, k) => {
                  const i = win.start + k;
                  return (
                    <div
                      key={t.trace_id}
                      id={`trace-row-${i}`}
                      role="row"
                      aria-rowindex={i + 2}
                      aria-selected={cursor === i}
                      className={`vrow clickable${cursor === i ? " active" : ""}`}
                      style={{ gridTemplateColumns: COLS, height: ROW_H }}
                      onClick={() => nav(`/p/${pid}/traces/${t.trace_id}`)}
                    >
                      <div role="gridcell" onClick={(e) => e.stopPropagation()} className="vcell check">
                        <input type="checkbox" aria-label={`Select trace ${t.name ?? t.trace_id}`} checked={selected.has(t.trace_id)} onChange={() => toggle(t.trace_id)} tabIndex={-1} />
                      </div>
                      <div role="gridcell" className="vcell small muted" title={new Date((t.start ?? 0) * 1000).toLocaleString()}>{fmtAgo(t.start)}</div>
                      <div role="gridcell" className="vcell" title={t.name ?? ""}>
                        {t.has_error && <span className="badge bad">error</span>}{" "}
                        <Link to={`/p/${pid}/traces/${t.trace_id}`} className="rowlink" onClick={(e) => e.stopPropagation()} tabIndex={-1}>
                          {t.name || <span className="muted">(unnamed)</span>}
                        </Link>
                      </div>
                      <div role="gridcell" className="vcell" title={preview(t.input, 400)}>{preview(t.input, 200) || <span className="muted">—</span>}</div>
                      <div role="gridcell" className="vcell" title={preview(t.output, 400)}>{preview(t.output, 200) || <span className="muted">—</span>}</div>
                      <div role="gridcell" className="vcell num">{fmtMs(t.duration_ms)}</div>
                      <div role="gridcell" className="vcell num">{(t.input_tokens ?? 0) + (t.output_tokens ?? 0) ? fmtNum((t.input_tokens ?? 0) + (t.output_tokens ?? 0)) : "—"}</div>
                      <div role="gridcell" className="vcell num">{fmtCost(t.cost_usd)}</div>
                      <div role="gridcell" className="vcell small" title={t.user_id ?? ""}>{t.user_id || <span className="muted">—</span>}</div>
                    </div>
                  );
                })}
              </div>
            </div>
            {traces.isLoading && <div className="empty">Loading…</div>}
            {traces.data && !rows.length && (
              <div className={filtered ? "empty" : undefined}>
                {filtered ? (
                  <>
                    No traces match these filters. <button onClick={() => setParams(clearFilters(params), { replace: true })}>Clear filters</button>
                  </>
                ) : (
                  <EmptyState title="No traces yet" code={<TraceSnippetPanel pid={pid} />}>
                    <p>Add the lines below to your app. Traces show up here within seconds.</p>
                  </EmptyState>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="row small muted" style={{ marginTop: 8 }}>
        <span>
          {rows.length ? `${fmtNum(rows.length)} of ${fmtNum(total)}${loadedAll ? "" : isFetchingNextPage ? " (loading more…)" : ""}` : ""}
        </span>
        <div className="spacer" />
        <span className="hint">
          <span className="kbd">j</span> <span className="kbd">k</span> move · <span className="kbd">Enter</span> open · <span className="kbd">x</span> select · <span className="kbd">/</span> search
        </span>
      </div>
    </div>
  );
}
