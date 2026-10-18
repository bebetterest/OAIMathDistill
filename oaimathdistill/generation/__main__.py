"""Command line: list, preview, run, and export."""

import argparse
import json
from pathlib import Path
import sys

from .methods import load_methods
from .runner import Plan, export_run, run


def preview_summary(value):
    overview = value["overview"]
    lines = [f"Output: {value['output']}", f"Fingerprint: {value['fingerprint']}",
             f"Output status: {overview['output_status']}",
             f"Jobs: {overview['jobs']} | Candidate limit: {overview['candidate_limit']} | Concurrency: {overview['concurrency']}",
             f"Generation: {overview['generation_backend']} | Review: {overview['review_backend']}"]
    limit = overview["maximum_calls_before_retries"]
    lines.append(f"Application calls before retries: at most {limit}" if limit is not None else
                 "Application call bound: custom methods determine their own calls")
    for name, backend in overview["backends"].items():
        lines.append(f"Backend {name}: {backend['kind']}, model={backend['model']}, timeout={backend['timeout']}s")
    lines.extend(["", "METHOD\tSOURCE\tINPUT\tFORM\tLIMIT"])
    for job in value["jobs"]:
        lines.append(f"{job['method']}\t{job['source_id']}\t{job['input_mode']}\t{job['params'].get('form', '-')}\t{job['params']['count']}")
    lines.extend(["", "Zero candidates is valid. This preview made no model calls and wrote no files.",
                  "Omit --summary to inspect the complete prompts and output schemas."])
    if overview["output_status"] in {"fingerprint_mismatch", "occupied", "invalid_manifest"}:
        lines.append("Choose a fresh output directory with --output runs/new-name before running.")
    elif overview["output_status"] == "locked":
        lines.append("This directory is locked. Inspect its owner before running or exporting.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List built-in methods and optional external method files")
    listing.add_argument("--method-file", type=Path, action="append", default=[])
    for name in ("preview", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--output", type=Path)
        if name == "run":
            command.add_argument("--retry-failed", action="store_true", help="Retry unfinished calls, preserving completed steps")
        else:
            command.add_argument("--summary", action="store_true", help="Show a concise job/backend/call overview without prompts")
    exporter = commands.add_parser("export")
    exporter.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "list":
            for name, method in load_methods(args.method_file).items():
                print(f"{name}\t{method.description}")
            return 0
        if args.command == "export":
            result = export_run(args.output.resolve())
        else:
            plan = Plan(args.config, args.output)
            result = plan.preview() if args.command == "preview" else run(plan, retry_failed=args.retry_failed)
        print(preview_summary(result) if args.command == "preview" and args.summary else
              json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command in {"run", "export"} and result.get("job_statuses", {}).get("failed", 0) else 0
    except KeyboardInterrupt:
        print("Interrupted; completed call checkpoints are retained.", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
