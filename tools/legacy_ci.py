#!/usr/bin/env python3
"""Plan, dispatch, and summarize exhaustive legacy V8 GitHub Actions runs."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


RUN_URL_RE = re.compile(r"/actions/runs/(?P<run_id>\d+)/?$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_batches(records: list[dict], batch_size: int = 3) -> list[dict]:
    if not 1 <= batch_size <= 3:
        raise ValueError("batch_size must be between 1 and 3")
    batches: list[dict] = []
    current_key = None
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        while current:
            versions, current = current[:batch_size], current[batch_size:]
            batches.append(
                {
                    "index": len(batches),
                    "group": {
                        "major": int(versions[0].split(".", 1)[0]),
                        "api_family": active_family,
                        "in_tree_gyp": versions[0].startswith("5.1."),
                    },
                    "versions": versions,
                    "dispatches": [],
                }
            )

    active_family = ""
    for record in records:
        version = record["version"]
        family = record["family"]
        key = (
            int(version.split(".", 1)[0]),
            family,
            version.startswith("5.1."),
        )
        if current_key is not None and key != current_key:
            flush()
        if key != current_key:
            current_key = key
            active_family = family
        current.append(version)
    flush()
    return batches


def audit_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_manifest(
    audit_path: Path,
    repo: str,
    branch: str,
    workflow: str,
    batch_size: int,
) -> dict:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    records = audit["versions"]
    batches = plan_batches(records, batch_size)
    return {
        "schema": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "repository": repo,
        "branch": branch,
        "workflow": workflow,
        "batch_size": batch_size,
        "source_audit": str(audit_path).replace("\\", "/"),
        "source_audit_sha256": audit_digest(audit_path),
        "summary": {
            "versions": len(records),
            "batches": len(batches),
            "dispatched": 0,
            "successful": 0,
            "failed": 0,
            "active": 0,
            "verified_versions": 0,
        },
        "batches": batches,
    }


def load_or_create_manifest(args: argparse.Namespace) -> dict:
    if args.manifest.is_file():
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if manifest["source_audit_sha256"] != audit_digest(args.audit):
            if any(batch.get("dispatches") for batch in manifest.get("batches", [])):
                raise RuntimeError(
                    "manifest audit digest changed after dispatch; use a new manifest"
                )
            return create_manifest(
                args.audit, args.repo, args.branch, args.workflow, args.batch_size
            )
        return manifest
    return create_manifest(
        args.audit, args.repo, args.branch, args.workflow, args.batch_size
    )


def save_manifest(path: Path, manifest: dict) -> None:
    manifest["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def require_pushed_clean_head(branch: str) -> str:
    if git_output("status", "--porcelain"):
        raise RuntimeError("refusing to dispatch from a dirty worktree")
    head = git_output("rev-parse", "HEAD")
    remote = git_output("rev-parse", f"refs/remotes/origin/{branch}")
    if head != remote:
        raise RuntimeError(f"HEAD {head} is not pushed to origin/{branch} ({remote})")
    return head


def parse_run_url(output: str) -> tuple[int, str]:
    url = output.strip().splitlines()[-1]
    match = RUN_URL_RE.search(url)
    if not match:
        raise RuntimeError(f"gh did not return an Actions run URL: {output!r}")
    return int(match.group("run_id")), url


def recent_workflow_runs(
    repo: str,
    workflow: str,
    branch: str,
    attempts: int = 3,
    retry_delay: float = 1.0,
) -> list[dict]:
    command = [
        "gh",
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        workflow,
        "--branch",
        branch,
        "--event",
        "workflow_dispatch",
        "--limit",
        "100",
        "--json",
        "databaseId,url,headSha,displayTitle",
    ]
    for attempt in range(attempts):
        try:
            completed = subprocess.run(
                command,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            return json.loads(completed.stdout)
        except subprocess.CalledProcessError:
            if attempt + 1 == attempts:
                raise
            time.sleep(retry_delay * (2**attempt))
    raise AssertionError("unreachable")


def select_dispatched_run(
    runs: list[dict], before_ids: set[int], head: str, display_title: str
) -> dict | None:
    matches = [
        run
        for run in runs
        if run["databaseId"] not in before_ids
        and run["headSha"] == head
        and run["displayTitle"] == display_title
    ]
    return max(matches, key=lambda run: run["databaseId"], default=None)


def wait_for_dispatched_run(
    repo: str,
    workflow: str,
    branch: str,
    before_ids: set[int],
    head: str,
    display_title: str,
    attempts: int = 15,
) -> tuple[int, str]:
    for attempt in range(attempts):
        match = select_dispatched_run(
            recent_workflow_runs(repo, workflow, branch),
            before_ids,
            head,
            display_title,
        )
        if match:
            return int(match["databaseId"]), match["url"]
        if attempt + 1 < attempts:
            time.sleep(2)
    raise RuntimeError(
        "workflow dispatch was accepted but its run could not be located; "
        "inspect Actions before retrying to avoid a duplicate batch"
    )


def dispatch(args: argparse.Namespace, manifest: dict) -> None:
    head = require_pushed_clean_head(args.branch)
    selected = manifest["batches"][
        args.start_batch : args.start_batch + args.count
    ]
    if not selected:
        raise RuntimeError("the selected batch range is empty")
    for batch in selected:
        if batch["dispatches"] and not args.force:
            print(f"[skip] batch {batch['index']} already has a dispatch")
            continue
        versions = batch["versions"]
        compact = json.dumps(versions, separators=(",", ":"))
        before_ids = {
            int(run["databaseId"])
            for run in recent_workflow_runs(args.repo, args.workflow, args.branch)
        }
        command = [
            "gh",
            "workflow",
            "run",
            args.workflow,
            "--repo",
            args.repo,
            "--ref",
            args.branch,
            "-f",
            f"version={versions[0]}",
            "-f",
            f"versions_json={compact}",
            "-f",
            "audit_only=true",
        ]
        print(
            f"[dispatch] batch {batch['index']}: "
            f"{versions[0]} through {versions[-1]} ({len(versions)} tags)"
        )
        completed = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            run_id, url = parse_run_url(completed.stdout)
        except RuntimeError:
            run_id, url = wait_for_dispatched_run(
                args.repo,
                args.workflow,
                args.branch,
                before_ids,
                head,
                f"Legacy V8 {versions[0]}",
            )
        batch["dispatches"].append(
            {
                "run_id": run_id,
                "url": url,
                "head_sha": head,
                "dispatched_at": utc_now(),
                "status": "queued",
                "conclusion": "",
                "jobs": [],
            }
        )
        save_manifest(args.manifest, manifest)
        print(f"[dispatch] {url}")


def view_run(
    repo: str,
    dispatch_record: dict,
    attempts: int = 3,
    retry_delay: float = 1.0,
) -> tuple[dict, dict]:
    """Read one run, tolerating short-lived GitHub API/indexing failures."""
    command = [
        "gh",
        "run",
        "view",
        str(dispatch_record["run_id"]),
        "--repo",
        repo,
        "--json",
        "status,conclusion,headSha,jobs,url",
    ]
    for attempt in range(attempts):
        try:
            completed = subprocess.run(
                command,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            return dispatch_record, json.loads(completed.stdout)
        except subprocess.CalledProcessError:
            if attempt + 1 == attempts:
                raise
            time.sleep(retry_delay * (2**attempt))
    raise AssertionError("unreachable")


def refreshable_dispatches(manifest: dict) -> list[dict]:
    """Return only latest run records whose completed state can still change."""
    return [
        batch["dispatches"][-1]
        for batch in manifest["batches"]
        if batch["dispatches"]
        and batch["dispatches"][-1].get("status") != "completed"
    ]


def refresh(manifest: dict, workers: int) -> None:
    latest = refreshable_dispatches(manifest)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(view_run, manifest["repository"], record)
            for record in latest
        ]
        for future in concurrent.futures.as_completed(futures):
            record, result = future.result()
            record.update(
                {
                    "url": result["url"],
                    "head_sha": result["headSha"],
                    "status": result["status"],
                    "conclusion": result["conclusion"],
                    "jobs": [
                        {
                            "name": job["name"],
                            "status": job["status"],
                            "conclusion": job["conclusion"],
                            "url": job["url"],
                        }
                        for job in result["jobs"]
                    ],
                }
            )
    update_summary(manifest)


def dispatch_has_failed_job(record: dict) -> bool:
    """Surface a terminal job failure before its sibling job finishes."""
    return any(
        job.get("status") == "completed"
        and job.get("conclusion") not in {"success", "skipped", "neutral"}
        for job in record.get("jobs", [])
    )


def update_summary(manifest: dict) -> None:
    latest = [
        (batch, batch["dispatches"][-1])
        for batch in manifest["batches"]
        if batch["dispatches"]
    ]
    successful = [item for item in latest if item[1]["conclusion"] == "success"]
    failed = [
        item
        for item in latest
        if (
            item[1]["status"] == "completed"
            and item[1]["conclusion"] != "success"
        )
        or dispatch_has_failed_job(item[1])
    ]
    manifest["summary"].update(
        {
            "dispatched": len(latest),
            "successful": len(successful),
            "failed": len(failed),
            "active": sum(item[1]["status"] != "completed" for item in latest),
            "verified_versions": sum(len(item[0]["versions"]) for item in successful),
        }
    )


def write_markdown(path: Path, manifest: dict) -> None:
    summary = manifest["summary"]
    scope_name = (
        "Modern V8"
        if "modern" in manifest.get("source_audit", "").lower()
        else "Legacy V8"
    )
    lines = [
        f"# {scope_name} GitHub Actions audit",
        "",
        f"Exact tags: **{summary['versions']}**",
        "",
        f"Verified on Linux and Windows: **{summary['verified_versions']}**",
        "",
        f"Batches: **{summary['successful']} successful**, "
        f"**{summary['failed']} failed**, **{summary['active']} active**",
        "",
        "| Batch | Exact tags | Run | Conclusion |",
        "|---:|---|---|---|",
    ]
    for batch in manifest["batches"]:
        versions = batch["versions"]
        version_text = ", ".join(versions)
        if batch["dispatches"]:
            run = batch["dispatches"][-1]
            run_text = f"[{run['run_id']}]({run['url']})"
            conclusion = run["conclusion"] or run["status"]
        else:
            run_text = "not dispatched"
            conclusion = "planned"
        lines.append(
            f"| {batch['index']} | {version_text} | {run_text} | {conclusion} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "dispatch", "refresh"))
    parser.add_argument("--audit", type=Path, default=Path("compat/legacy-v8-api.json"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--repo", default="xqy2006/jsc2js")
    parser.add_argument("--branch", default="v12-legacy-support")
    parser.add_argument("--workflow", default="compile.yml")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--start-batch", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_or_create_manifest(args)
    if args.command == "dispatch":
        dispatch(args, manifest)
        update_summary(manifest)
    elif args.command == "refresh":
        refresh(manifest, args.workers)
    else:
        update_summary(manifest)
    save_manifest(args.manifest, manifest)
    if args.markdown:
        write_markdown(args.markdown, manifest)
    print(json.dumps(manifest["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
