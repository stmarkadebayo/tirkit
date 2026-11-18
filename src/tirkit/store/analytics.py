"""Sessions, users, filter values and dashboard metrics."""

from __future__ import annotations

from typing import Any

from tirkit.store.base import StoreBase


def _pct(sorted_values: list[float], q: float) -> float:
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _num_row(row: dict[str, Any]) -> dict[str, Any]:
    """Postgres returns Decimal for SUM(bigint); make rows JSON-friendly."""
    from decimal import Decimal

    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


class AnalyticsMixin(StoreBase):
    def sessions(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT session_id, COUNT(*), MIN(start_time), MAX(end_time), SUM(cost_usd), "
            "SUM(input_tokens), SUM(output_tokens), MAX(has_error), MAX(user_id) FROM traces "
            "WHERE project_id = ? AND session_id IS NOT NULL GROUP BY session_id "
            "ORDER BY MAX(start_time) DESC LIMIT ? OFFSET ?",
            [self.project_id, limit, offset],
        )
        keys = "session_id n_traces start end cost_usd input_tokens output_tokens has_error user_id"
        return [_num_row(dict(zip(keys.split(), r, strict=True))) for r in rows]

    def users(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT user_id, COUNT(*), MIN(start_time), MAX(start_time), SUM(cost_usd), "
            "SUM(input_tokens), SUM(output_tokens), SUM(has_error) FROM traces "
            "WHERE project_id = ? AND user_id IS NOT NULL GROUP BY user_id "
            "ORDER BY MAX(start_time) DESC LIMIT ? OFFSET ?",
            [self.project_id, limit, offset],
        )
        keys = "user_id n_traces first_seen last_seen cost_usd input_tokens output_tokens n_errors"
        return [_num_row(dict(zip(keys.split(), r, strict=True))) for r in rows]

    def filter_values(self) -> dict[str, list[Any]]:
        """Distinct values for UI filter menus."""
        out: dict[str, list[Any]] = {}
        for key, table, col in (
            ("names", "traces", "name"),
            ("span_names", "spans", "name"),
            ("models", "spans", "model"),
            ("releases", "traces", "release"),
            ("kinds", "spans", "kind"),
        ):
            rows = self.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE project_id = ? AND {col} IS NOT NULL "
                f"ORDER BY {col} LIMIT 500",
                [self.project_id],
            )
            out[key] = [r[0] for r in rows]
        rows = self.execute(
            "SELECT DISTINCT name FROM scores WHERE project_id = ? ORDER BY name LIMIT 500",
            [self.project_id],
        )
        out["scores"] = [r[0] for r in rows]
        return out

    def metrics(
        self,
        since: float,
        until: float,
        *,
        buckets: int = 48,
        group_by: str | None = None,
        max_latency_samples: int = 100_000,
    ) -> dict[str, Any]:
        """Dashboard series over [since, until): per bucket and optional group (model, name,
        release, user_id, kind): spans, traces, errors, tokens, cost, latency p50/p95/p99,
        prompt-cache hit rate, reasoning tokens, time to first token p50/p95, and score
        averages. Latency percentiles are exact up to ``max_latency_samples`` traces
        (SPEC §6.8); the sample size is reported."""
        width = max((until - since) / buckets, 1.0)
        group_col = {
            "model": "model",
            "name": "name",
            "release": "release",
            "user_id": "user_id",
            "kind": "kind",
        }.get(group_by or "")
        g = f"COALESCE({group_col}, '(none)')" if group_col else "'all'"
        bucket = f"CAST((start_time - {float(since)!r}) / {width!r} AS INTEGER)"
        d = self.dialect
        cached = f"CAST({d.json_get('usage', 'cached_input_tokens')} AS {d.real})"
        reasoning = f"CAST({d.json_get('usage', 'reasoning_tokens')} AS {d.real})"
        rows = self.execute(
            f"SELECT {bucket} AS b, {g} AS g, COUNT(*), "
            "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), SUM(input_tokens), "
            f"SUM(output_tokens), SUM(cost_usd), SUM({cached}), SUM({reasoning}) FROM spans "
            "WHERE project_id = ? AND start_time >= ? AND start_time < ? "
            "GROUP BY 1, 2",
            [self.project_id, since, until],
        )
        series: dict[str, dict[int, dict[str, Any]]] = {}
        for b, grp, n, errs, tin, tout, cost, tcached, treason in rows:
            series.setdefault(str(grp), {})[int(b)] = {
                "spans": int(n),
                "errors": int(errs or 0),
                "input_tokens": int(tin or 0),
                "output_tokens": int(tout or 0),
                "cost_usd": float(cost or 0.0),
                "cached_input_tokens": int(tcached or 0),
                "reasoning_tokens": int(treason or 0),
                # Share of input tokens served from the provider's prompt cache.
                "cache_hit_rate": float(tcached or 0) / float(tin) if tin else None,
            }
        ttft_rows = self.execute(
            f"SELECT {bucket}, {g}, ttft_ms FROM spans "
            "WHERE project_id = ? AND start_time >= ? AND start_time < ? "
            "AND ttft_ms IS NOT NULL ORDER BY start_time DESC LIMIT ?",
            [self.project_id, since, until, max_latency_samples],
        )
        ttft: dict[tuple[str, int], list[float]] = {}
        for b, grp, value in ttft_rows:
            ttft.setdefault((str(grp), int(b)), []).append(float(value))
        for (grp, b), values in ttft.items():
            values.sort()
            point = series.setdefault(grp, {}).setdefault(b, {"spans": 0, "errors": 0})
            point.update(ttft_p50_ms=_pct(values, 0.5), ttft_p95_ms=_pct(values, 0.95))
        trace_group = group_col if group_col in {"name", "release", "user_id"} else None
        tg = f"COALESCE({trace_group}, '(none)')" if trace_group else "'all'"
        lat_rows = self.execute(
            f"SELECT {bucket}, {tg}, duration_ms, has_error FROM traces "
            "WHERE project_id = ? AND start_time >= ? AND start_time < ? "
            "AND duration_ms IS NOT NULL ORDER BY start_time DESC LIMIT ?",
            [self.project_id, since, until, max_latency_samples],
        )
        lat: dict[tuple[str, int], list[float]] = {}
        tcount: dict[tuple[str, int], list[int]] = {}
        for b, grp, dur, err in lat_rows:
            lat.setdefault((str(grp), int(b)), []).append(float(dur))
            c = tcount.setdefault((str(grp), int(b)), [0, 0])
            c[0] += 1
            c[1] += int(err or 0)
        for (grp, b), durs in lat.items():
            durs.sort()
            point = series.setdefault(grp, {}).setdefault(b, {"spans": 0, "errors": 0})
            point.update(
                traces=tcount[(grp, b)][0],
                trace_errors=tcount[(grp, b)][1],
                p50_ms=_pct(durs, 0.5),
                p95_ms=_pct(durs, 0.95),
                p99_ms=_pct(durs, 0.99),
            )
        score_rows = self.execute(
            f"SELECT CAST((created - {float(since)!r}) / {width!r} AS INTEGER), name, AVG(value), "
            "COUNT(value) FROM scores WHERE project_id = ? AND created >= ? AND created < ? "
            "AND value IS NOT NULL AND experiment_id IS NULL GROUP BY 1, name",
            [self.project_id, since, until],
        )
        scores: dict[str, dict[int, dict[str, float]]] = {}
        for b, name, avg, n in score_rows:
            scores.setdefault(str(name), {})[int(b)] = {"mean": float(avg), "n": int(n)}
        return {
            "since": since,
            "until": until,
            "bucket_s": width,
            "buckets": buckets,
            "group_by": group_col,
            "series": {
                grp: [{"bucket": b, "t": since + b * width, **v} for b, v in sorted(pts.items())]
                for grp, pts in series.items()
            },
            "scores": {
                name: [{"bucket": b, "t": since + b * width, **v} for b, v in sorted(pts.items())]
                for name, pts in scores.items()
            },
            "latency_sample_size": len(lat_rows),
            "latency_sample_capped": len(lat_rows) >= max_latency_samples,
        }
