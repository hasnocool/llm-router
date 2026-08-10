# src/llm_router/metrics_report.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .metrics_db import MetricsDB


class MetricsReportGenerator:
    """Generate a Markdown snapshot from an injected MetricsDB instance."""

    def __init__(self, report_path: Path, db: MetricsDB):
        self.report_path = report_path
        self.db = db

    def generate_report(self, providers: list[str], days: int = 7) -> str:
        all_metrics = {
            provider: self._get_provider_report_data(provider, days) for provider in providers
        }
        return self._render_markdown(all_metrics, days)

    def _get_provider_report_data(self, provider: str, days: int) -> dict[str, Any]:
        return {
            "provider": provider,
            "daily": self.db.get_daily_metrics(provider, days),
            "today": self.db.get_today_metrics(provider),
            "quota_remaining": self.db.get_remaining_quota(provider),
            "rate_limits": self.db.get_rate_limits(provider),
            "quota": self.db.get_quota(provider),
        }

    def _render_markdown(self, all_metrics: dict[str, dict], days: int) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "# LLM Router Provider Metrics Report",
            "",
            f"**Generated:** {timestamp}",
            f"**Period:** Last {days} days",
            f"**Database:** {self.db.db_path}",
            "",
            "## Summary",
            "",
            "| Provider | Calls Today | Calls Remaining | Tokens Today | Tokens Remaining | p50 Latency | p99 Latency |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for provider, data in sorted(all_metrics.items()):
            today = data["today"]
            quota = data["quota_remaining"]
            lines.append(
                "| {provider} | {calls} | {calls_remaining} | {tokens} | {tokens_remaining} | {p50:.1f}ms | {p99:.1f}ms |".format(
                    provider=provider,
                    calls=today.api_calls_total if today else 0,
                    calls_remaining=_fmt(quota.get("requests_remaining")),
                    tokens=today.total_tokens if today else 0,
                    tokens_remaining=_fmt(quota.get("tokens_remaining")),
                    p50=today.latency_p50_ms if today else 0.0,
                    p99=today.latency_p99_ms if today else 0.0,
                )
            )

        lines.extend(["", "## Provider Details", ""])
        for provider, data in sorted(all_metrics.items()):
            lines.extend(self._render_provider_section(provider, data))
        lines.extend(["---", f"*Report generated at {timestamp}*"])
        return "\n".join(lines)

    def _render_provider_section(self, provider: str, data: dict) -> list[str]:
        today = data["today"]
        quota_remaining = data["quota_remaining"]
        quota = data["quota"]
        rate_limits = data["rate_limits"]
        daily = data["daily"]
        lines = [f"### {provider}", ""]
        if today:
            lines.extend([
                f"- Calls today: **{today.api_calls_total}** ({today.api_calls_failed} failed)",
                f"- Tokens today: **{today.total_tokens}**",
                f"- Latency p50/p99: **{today.latency_p50_ms:.1f} / {today.latency_p99_ms:.1f} ms**",
            ])
        else:
            lines.append("- No requests recorded today")
        if quota:
            lines.extend([
                f"- Local guard: {_fmt(quota.daily_request_limit)} requests / {_fmt(quota.daily_token_limit)} tokens",
                f"- Guard reset: {quota.quota_reset_hour:02d}:00 UTC",
                f"- Remaining: {_fmt(quota_remaining.get('requests_remaining'))} requests / {_fmt(quota_remaining.get('tokens_remaining'))} tokens",
            ])
        lines.extend(["", "#### Provider rate-limit windows", ""])
        if rate_limits:
            lines.extend(["| Type | Limit | Remaining | Reset | Header |", "|---|---:|---:|---|---|"])
            for limit in rate_limits:
                lines.append(
                    f"| {limit.limit_type} | {_fmt(limit.limit_value)} | {_fmt(limit.remaining)} | {_fmt_reset(limit.reset_timestamp)} | {limit.header_source or '-'} |"
                )
        else:
            lines.append("No rate-limit headers captured yet.")
        lines.extend(["", f"#### Last {len(daily)} recorded days", ""])
        if daily:
            lines.extend(["| Date | Calls | Failed | Tokens | p50 | p99 |", "|---|---:|---:|---:|---:|---:|"])
            for item in daily:
                lines.append(
                    f"| {item.metric_date} | {item.api_calls_total} | {item.api_calls_failed} | {item.total_tokens} | {item.latency_p50_ms:.1f}ms | {item.latency_p99_ms:.1f}ms |"
                )
        lines.append("")
        return lines

    def write_report(self, providers: list[str], days: int = 7) -> Path:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(self.generate_report(providers, days), encoding="utf-8")
        return self.report_path


def _fmt(value: object) -> str:
    return "N/A" if value is None else str(value)


def _fmt_reset(value: int | None) -> str:
    if value is None:
        return "unknown"
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
