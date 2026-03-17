"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn

from ui_knowledge_service.app import create_app
from ui_knowledge_service.config import Settings
from ui_knowledge_service.mcp_server import build_mcp_server
from ui_knowledge_service.models import AuditSeverity, RefreshRequest
from ui_knowledge_service.service import KnowledgeService


def main() -> None:
    parser = argparse.ArgumentParser(description="UI knowledge service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI + MCP server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("stdio", help="Run the MCP server over stdio")
    subparsers.add_parser("prewarm", help="Populate the starter offline cache")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh one component")
    refresh_parser.add_argument("library")
    refresh_parser.add_argument("component")
    refresh_parser.add_argument("--doc-type", default=None)
    refresh_parser.add_argument("--force", action="store_true")

    audit_parser = subparsers.add_parser("audit-catalog", help="Fetch and validate catalog sources")
    audit_parser.add_argument("--library", default=None)
    audit_parser.add_argument("--component", default=None)
    audit_parser.add_argument("--limit", type=int, default=None)
    audit_parser.add_argument("--snapshot-dir", default=None)
    audit_parser.add_argument("--baseline-path", default=None)
    audit_parser.add_argument("--compare-to-baseline", action="store_true")
    audit_parser.add_argument("--write-baseline", action="store_true")
    audit_parser.add_argument("--fail-on-drift", action="store_true")
    audit_parser.add_argument(
        "--fail-on-severity",
        choices=[severity.value for severity in AuditSeverity],
        default=None,
        help="Exit non-zero if the maintenance report contains recommendations at or above this severity.",
    )
    audit_parser.add_argument(
        "--markdown-report",
        default=None,
        help="Write the maintenance report as Markdown to the given path.",
    )

    promote_parser = subparsers.add_parser("promote-baseline", help="Safely promote the current audit to the baseline")
    promote_parser.add_argument("--library", default=None)
    promote_parser.add_argument("--component", default=None)
    promote_parser.add_argument("--limit", type=int, default=None)
    promote_parser.add_argument("--baseline-path", default=None)
    promote_parser.add_argument("--snapshot-dir", default=None)
    promote_parser.add_argument("--report-dir", default=None)
    promote_parser.add_argument(
        "--max-allowed-severity",
        choices=[severity.value for severity in AuditSeverity],
        default=AuditSeverity.warn.value,
        help="Block promotion when recommendations at or above this severity are present.",
    )
    promote_parser.add_argument("--force", action="store_true", help="Promote even when blocking recommendations exist.")

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "serve":
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    if args.command == "stdio":
        asyncio.run(_run_stdio_command(settings))
        return

    asyncio.run(_run_async_command(args, settings))


def main_stdio() -> None:
    """Console script entrypoint that runs the MCP server over stdio."""

    settings = Settings.from_env()
    asyncio.run(_run_stdio_command(settings))


async def _run_async_command(args: argparse.Namespace, settings: Settings) -> None:
    service = KnowledgeService(settings)
    await service.startup()
    try:
        if args.command == "prewarm":
            result = await service.prewarm(force=False)
            print(result.model_dump_json(indent=2))
        elif args.command == "refresh":
            result = await service.refresh(
                RefreshRequest(
                    library=args.library,
                    component=args.component,
                    doc_type=args.doc_type,
                    force=args.force,
                )
            )
            print(result.model_dump_json(indent=2))
        elif args.command == "audit-catalog":
            report = await service.audit_sources(
                library=args.library,
                component=args.component,
                limit=args.limit,
                snapshot_dir=args.snapshot_dir,
            )
            payload: dict[str, object] = {"report": report.model_dump(mode="json")}
            comparison = None
            if args.compare_to_baseline:
                baseline = service.load_audit_baseline(baseline_path=args.baseline_path)
                if baseline is None:
                    payload["comparison"] = None
                    payload["baseline_path"] = args.baseline_path or str(service.default_audit_baseline_path())
                    payload["message"] = "No baseline file was found."
                else:
                    comparison = service.compare_audit_reports(report, baseline)
                    payload["comparison"] = comparison.model_dump(mode="json")
                    payload["baseline_path"] = args.baseline_path or str(service.default_audit_baseline_path())
                    if args.fail_on_drift and has_drift_entries(comparison.entries):
                        payload["maintenance_report"] = service.generate_audit_maintenance_report(
                            report,
                            comparison=comparison,
                            baseline_path=payload["baseline_path"],
                        ).model_dump(mode="json")
                        print(json_dumps(payload))
                        raise SystemExit(1)
            maintenance_report = service.generate_audit_maintenance_report(
                report,
                comparison=comparison,
                baseline_path=payload.get("baseline_path"),
            )
            payload["maintenance_report"] = maintenance_report.model_dump(mode="json")
            if args.markdown_report:
                markdown = service.render_audit_maintenance_report_markdown(maintenance_report)
                with open(args.markdown_report, "w", encoding="utf-8") as handle:
                    handle.write(markdown)
                payload["markdown_report_path"] = args.markdown_report
            if args.write_baseline:
                payload["baseline_path"] = service.save_audit_baseline(report, baseline_path=args.baseline_path)
                payload["maintenance_report"]["baseline_path"] = payload["baseline_path"]
            if args.fail_on_severity and has_recommendations_at_or_above(
                maintenance_report.recommendations,
                AuditSeverity(args.fail_on_severity),
            ):
                print(json_dumps(payload))
                raise SystemExit(1)
            print(json_dumps(payload))
        elif args.command == "promote-baseline":
            report, comparison, promotion = await service.promote_audit_baseline(
                library=args.library,
                component=args.component,
                limit=args.limit,
                baseline_path=args.baseline_path,
                snapshot_dir=args.snapshot_dir,
                report_dir=args.report_dir,
                max_allowed_severity=AuditSeverity(args.max_allowed_severity),
                force=args.force,
            )
            payload = {
                "report": report.model_dump(mode="json"),
                "comparison": comparison.model_dump(mode="json") if comparison else None,
                "promotion": promotion.model_dump(mode="json"),
            }
            print(json_dumps(payload))
            if not promotion.promoted:
                raise SystemExit(1)
    finally:
        await service.shutdown()


def json_dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2)


def has_drift_entries(entries) -> bool:
    for entry in entries:
        status = entry.get("status") if isinstance(entry, dict) else getattr(entry, "status", None)
        if status != "unchanged":
            return True
    return False


def has_recommendations_at_or_above(recommendations, threshold: AuditSeverity) -> bool:
    threshold_rank = severity_rank(threshold)
    for recommendation in recommendations:
        severity = recommendation.get("severity") if isinstance(recommendation, dict) else getattr(recommendation, "severity", None)
        if severity is None:
            continue
        severity_value = severity if isinstance(severity, str) else severity.value
        if severity_rank(AuditSeverity(severity_value)) >= threshold_rank:
            return True
    return False


def severity_rank(severity: AuditSeverity) -> int:
    return {
        AuditSeverity.info: 1,
        AuditSeverity.warn: 2,
        AuditSeverity.error: 3,
    }[severity]


async def _run_stdio_command(
    settings: Settings,
    *,
    service: KnowledgeService | None = None,
    mcp_factory=build_mcp_server,
) -> None:
    active_service = service or KnowledgeService(settings)
    await active_service.startup()
    try:
        mcp_server = mcp_factory(active_service)
        await mcp_server.run_stdio_async()
    finally:
        await active_service.shutdown()
