from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .metrics_db import get_metrics_db


class MetricsReportGenerator:
    """Generates markdown reports from provider metrics."""

    def __init__(self, report_path: Path, config_path: Path | None = None):
        self.report_path = report_path
        self.db = get_metrics_db(config_path=config_path)

    def generate_report(self, days: int = 7) -> str:
        """Generate a full markdown report."""
        all_metrics = {}
        for provider_name in self._get_all_providers():
            all_metrics[provider_name] = self._get_provider_report_data(provider_name, days)

        return self._render_markdown(all_metrics, days)

    def _get_all_providers(self) -> list[str]:
        """Get all provider names from quotas and metrics tables."""
        providers = set()
        # From quotas
        quotas = self.db.get_all_quotas()
        for q in quotas:
            providers.add(q.provider_name)
        # From daily metrics (recent)
        conn = self.db._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT provider_name FROM provider_daily_metrics WHERE metric_date >= ?",
            ((date.today()).isoformat(),),
        ).fetchall()
        for r in rows:
            providers.add(r["provider_name"])
        # From rate limits
        rate_limits = self.db.get_all_rate_limits()
        for rl in rate_limits:
            providers.add(rl.provider_name)
        return sorted(providers)

    def _get_provider_report_data(self, provider: str, days: int) -> dict[str, Any]:
        """Get all data needed for a provider's report section."""
        daily = self.db.get_daily_metrics(provider, days)
        today = self.db.get_today_metrics(provider)
        quota_remaining = self.db.get_remaining_quota(provider)
        rate_limit = self.db.get_rate_limit(provider)
        quota = self.db.get_quota(provider)

        return {
            "provider": provider,
            "daily": daily,
            "today": today,
            "quota_remaining": quota_remaining,
            "rate_limit": rate_limit,
            "quota": quota,
        }

    def _render_markdown(self, all_metrics: dict[str, dict], days: int) -> str:
        """Render the full markdown report."""
        lines = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

        # Header
        lines.append("# LLM Router Provider Metrics Report")
        lines.append("")
        lines.append(f"**Generated:** {timestamp}")
        lines.append(f"**Period:** Last {days} days")
        lines.append(f"**Database:** {self.db.db_path}")
        lines.append("")

        # Summary table
        lines.append("## Summary")
        lines.append("")
        lines.append(
            "| Provider | Available | Calls Today | Calls Remaining | Tokens Today | Tokens Remaining | Rate Limit Remaining | Rate Limit Reset | p50 Latency | p99 Latency |"
        )
        lines.append(
            "|----------|-----------|-------------|-----------------|--------------|------------------|---------------------|------------------|-------------|-------------|"
        )

        for provider, data in sorted(all_metrics.items()):
            today = data["today"]
            qr = data["quota_remaining"]
            rl = data["rate_limit"]

            calls_today = today.api_calls_total if today else 0
            tokens_today = today.total_tokens if today else 0
            calls_rem = qr.get("requests_remaining") if qr.get("requests_remaining") is not None else "N/A"
            tokens_rem = qr.get("tokens_remaining") if qr.get("tokens_remaining") is not None else "N/A"
            rl_rem = rl.remaining if rl else "N/A"
            rl_reset = self._format_timestamp(rl.reset_timestamp) if rl and rl.reset_timestamp else "N/A"
            p50 = f"{today.latency_p50_ms:.1f}ms" if today and today.latency_count > 0 else "N/A"
            p99 = f"{today.latency_p99_ms:.1f}ms" if today and today.latency_count > 0 else "N/A"

            # Availability from rate limit (heuristic)
            available = "✅" if rl and rl.remaining and rl.remaining > 0 else "❓"

            lines.append(
                f"| {provider} | {available} | {calls_today} | {calls_rem} | {tokens_today} | {tokens_rem} | {rl_rem} | {rl_reset} | {p50} | {p99} |"
            )

        lines.append("")

        # Detailed sections per provider
        lines.append("## Provider Details")
        lines.append("")

        for provider, data in sorted(all_metrics.items()):
            lines.append(self._render_provider_section(provider, data))

        # Footer
        lines.append("---")
        lines.append(f"*Report generated at {timestamp}*")

        return "\n".join(lines)

    def _render_provider_section(self, provider: str, data: dict) -> str:
        """Render detailed section for a single provider."""
        lines = []
        today = data["today"]
        qr = data["quota_remaining"]
        rl = data["rate_limit"]
        quota = data["quota"]
        daily = data["daily"]

        lines.append(f"### {provider}")
        lines.append("")

        # Current status
        lines.append("#### Current Status")
        lines.append("")
        if today:
            lines.append(f"- **Calls Today:** {today.api_calls_total} ({today.api_calls_failed} failed)")
            lines.append(f"- **Tokens Today:** {today.total_tokens} ({today.prompt_tokens} prompt + {today.completion_tokens} completion)")
            if today.latency_count > 0:
                lines.append(f"- **Latency (p50):** {today.latency_p50_ms:.1f}ms")
                lines.append(f"- **Latency (p99):** {today.latency_p99_ms:.1f}ms")
        else:
            lines.append("- **No data for today**")

        # Quota
        lines.append("")
        lines.append("#### Quota Configuration")
        lines.append("")
        if quota:
            lines.append(f"- **Daily Request Limit:** {quota.daily_request_limit or 'Unlimited'}")
            lines.append(f"- **Daily Token Limit:** {quota.daily_token_limit or 'Unlimited'}")
            lines.append(f"- **Reset Hour (UTC):** {quota.quota_reset_hour}:00")
        else:
            lines.append("- **No quota configured**")

        lines.append("")
        lines.append("#### Quota Remaining")
        lines.append("")
        if qr.get("requests_remaining") is not None:
            lines.append(f"- **Requests:** {qr['requests_remaining']} / {quota.daily_request_limit if quota and quota.daily_request_limit else 'N/A'}")
        else:
            lines.append("- **Requests:** Unlimited / Not configured")

        if qr.get("tokens_remaining") is not None:
            lines.append(f"- **Tokens:** {qr['tokens_remaining']} / {quota.daily_token_limit if quota and quota.daily_token_limit else 'N/A'}")
        else:
            lines.append("- **Tokens:** Unlimited / Not configured")

        # Rate limit
        lines.append("")
        lines.append("#### Rate Limit (from provider headers)")
        lines.append("")
        if rl:
            lines.append(f"- **Type:** {rl.limit_type}")
            lines.append(f"- **Limit:** {rl.limit_value or 'Unknown'}")
            lines.append(f"- **Remaining:** {rl.remaining}")
            lines.append(f"- **Resets:** {self._format_timestamp(rl.reset_timestamp) if rl.reset_timestamp else 'Unknown'}")
            lines.append(f"- **Header Source:** {rl.header_source}")
            lines.append(f"- **Last Polled:** {rl.last_polled.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            lines.append("- **No rate limit data available**")

        # History
        lines.append("")
        lines.append(f"#### Last {len(daily)} Days History")
        lines.append("")
        if daily:
            lines.append("| Date | Calls | Failed | Prompt Tokens | Completion Tokens | Total Tokens | p50 Latency | p99 Latency |")
            lines.append("|------|-------|--------|---------------|-------------------|--------------|-------------|-------------|")
            for d in daily:
                lines.append(
                    f"| {d.metric_date} | {d.api_calls_total} | {d.api_calls_failed} | "
                    f"{d.prompt_tokens} | {d.completion_tokens} | {d.total_tokens} | "
                    f"{d.latency_p50_ms:.1f}ms | {d.latency_p99_ms:.1f}ms |"
                )
        else:
            lines.append("*No historical data*")

        lines.append("")
        return "\n".join(lines)

    def _format_timestamp(self, ts: int | None) -> str:
        if ts is None:
            return "N/A"
        dt = datetime.fromtimestamp(ts)
        now = datetime.now()
        diff = dt - now
        if diff.total_seconds() < 0:
            return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} (expired)"
        hours = int(diff.total_seconds() / 3600)
        minutes = int((diff.total_seconds() % 3600) / 60)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} (in {hours}h {minutes}m)"

    def write_report(self, days: int = 7) -> Path:
        """Generate and write report to file."""
        report = self.generate_report(days)
        self.report_path.write_text(report)
        return self.report_path


def generate_metrics_report(report_path: Path | str, config_path: Path | None = None, days: int = 7) -> Path:
    """Convenience function to generate a metrics report."""
    if isinstance(report_path, str):
        report_path = Path(report_path)
    generator = MetricsReportGenerator(report_path, config_path)
    return generator.write_report(days)