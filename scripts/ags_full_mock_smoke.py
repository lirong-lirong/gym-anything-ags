#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DEFAULT_DATA_ROOT = Path("/tmp/cua-data/cua_world_ags_output_osworld_format_no_heavyinstall")
DEFAULT_RUN_ROOT = Path("ags_mock_smoke_runs")
ENV_ROOT = REPO / "benchmarks/cua_world/environments"
DEFAULT_TARGET_ENVS = {
    "firefox_env",
    "libreoffice_calc_env",
    "libreoffice_impress_env",
    "libreoffice_writer_env",
    "thunderbird_env",
    "vlc_media_player_env",
    "vscode_env",
}


def load_tasks(
    data_root: Path,
    target_envs: Optional[set[str]] = None,
    limit_per_env: Optional[int] = None,
    only_tasks: Optional[set[tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    import pandas as pd

    rows: List[Dict[str, Any]] = []
    seen = set()
    env_counts: Counter = Counter()
    for split in ("train", "test"):
        parquet_path = data_root / f"{split}.parquet"
        df = pd.read_parquet(parquet_path)
        for _, row in df.iterrows():
            extra = row["extra_info"]
            if isinstance(extra, str):
                extra = json.loads(extra)
            env = row.get("env_name") or extra.get("domain")
            if target_envs and env not in target_envs:
                continue
            task_id = extra["task_id"]
            task_dir = task_id.split("@", 1)[0]
            if only_tasks is not None and (env, task_dir) not in only_tasks:
                continue
            key = (env, task_dir)
            if key in seen:
                continue
            seen.add(key)
            official_task = ENV_ROOT / env / "tasks" / task_dir / "task.json"
            if not official_task.exists():
                continue
            if limit_per_env is not None and env_counts[env] >= limit_per_env:
                continue
            env_counts[env] += 1
            rows.append(
                {
                    "split": split,
                    "env": env,
                    "task_id": task_id,
                    "task_dir": task_dir,
                    "index": extra.get("index"),
                    "difficulty": extra.get("difficulty"),
                }
            )
    return rows


def load_registry_tasks(
    *,
    surface: str,
    split: str,
    target_envs: Optional[set[str]] = None,
    limit_per_env: Optional[int] = None,
    only_tasks: Optional[set[tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    from benchmarks.cua_world.registry import load_environment_task_splits

    registry = load_environment_task_splits(surface=surface)
    rows: List[Dict[str, Any]] = []
    env_counts: Counter = Counter()
    for env in sorted(registry):
        if target_envs and env not in target_envs:
            continue
        task_ids = registry[env].get(split)
        if task_ids is None:
            available = ", ".join(sorted(registry[env]))
            raise SystemExit(f"Split {split!r} is not available for {env}; available: {available}")
        for task_dir in task_ids:
            if only_tasks is not None and (env, task_dir) not in only_tasks:
                continue
            official_task = ENV_ROOT / env / "tasks" / task_dir / "task.json"
            if not official_task.exists():
                continue
            if limit_per_env is not None and env_counts[env] >= limit_per_env:
                continue
            env_counts[env] += 1
            rows.append(
                {
                    "split": split,
                    "env": env,
                    "task_id": task_dir,
                    "task_dir": task_dir,
                    "index": None,
                    "difficulty": None,
                    "task_source": f"registry:{surface}",
                }
            )
    return rows


def _copy_file(src: str, dst: str) -> None:
    shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, copy_function=_copy_file)


def prepare_task_env(
    task: Dict[str, Any],
    run_root: Path,
    template: str,
    *,
    hook_timeout: Optional[int] = None,
    force: bool = False,
) -> Path:
    env = task["env"]
    task_dir = task["task_dir"]
    src_env = ENV_ROOT / env
    dst = run_root / "envs" / env / task_dir
    if dst.exists() and not force:
        return dst
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    data = json.loads((src_env / "env.json").read_text(encoding="utf-8"))
    data["runner"] = "ags"
    data["image"] = template
    data["diagnostics"] = True
    recording = data.setdefault("recording", {})
    recording["output_dir"] = str(run_root / "artifacts" / f"{env}__{task_dir}")

    rewritten_mounts = []
    for mount in data.get("mounts", []):
        source = Path(mount.get("source", ""))
        target = mount.get("target")
        mode = mount.get("mode", "ro")
        source_path = source if source.is_absolute() else (REPO / source)
        local_name = Path(target).name if target else source_path.name
        if target == "/workspace/tasks":
            local_name = "tasks"
            local_tasks = dst / local_name / task_dir
            local_tasks.parent.mkdir(parents=True, exist_ok=True)
            _copy_tree(src_env / "tasks" / task_dir, local_tasks)
        elif source_path.exists():
            local_target = dst / local_name
            if source_path.is_dir():
                _copy_tree(source_path, local_target)
            else:
                local_target.parent.mkdir(parents=True, exist_ok=True)
                _copy_file(str(source_path), str(local_target))
        else:
            # Docker bind mounts create a missing host directory in many local
            # runs; AGS uploads require a concrete local source, so mirror that
            # behavior with an empty directory for optional mounts.
            (dst / local_name).mkdir(parents=True, exist_ok=True)
        rewritten_mounts.append({"source": local_name, "target": target, "mode": mode})
    data["mounts"] = rewritten_mounts
    (dst / "env.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _patch_task_for_mock_verifier(dst, task_dir, hook_timeout=hook_timeout)
    return dst


def _container_path_to_local(env_dir: Path, container_path: str) -> Optional[Path]:
    prefix = "/workspace/"
    if not container_path.startswith(prefix):
        return None
    return env_dir / container_path[len(prefix):]


def _ensure_noop_hook(path: Path, name: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"echo 'mock {name}: no task-local script present in source tree'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_placeholder_asset(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        path.write_text(
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>Mock asset</title></head>"
            "<body><main><h1>Mock asset</h1><p>Generated for AGS smoke setup.</p></main></body></html>\n",
            encoding="utf-8",
        )
        return
    if suffix == ".csv":
        path.write_text("name,value\nmock,1\n", encoding="utf-8")
        return
    if suffix == ".odp":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.presentation", compress_type=zipfile.ZIP_STORED)
            archive.writestr(
                "META-INF/manifest.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">
  <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.presentation"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>
</manifest:manifest>
""",
            )
            archive.writestr(
                "content.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3">
  <office:body><office:presentation>
    <draw:page draw:name="Slide 1"><draw:frame draw:name="Title" draw:x="1cm" draw:y="1cm" draw:width="20cm" draw:height="4cm"><draw:text-box><text:p>Mock presentation asset</text:p></draw:text-box></draw:frame></draw:page>
    <draw:page draw:name="Slide 2"><draw:frame draw:name="Body" draw:x="1cm" draw:y="1cm" draw:width="20cm" draw:height="4cm"><draw:text-box><text:p>Generated for AGS smoke setup.</text:p></draw:text-box></draw:frame></draw:page>
  </office:presentation></office:body>
</office:document-content>
""",
            )
            archive.writestr(
                "styles.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" office:version="1.3"/>
""",
            )
        return
    path.write_text("mock asset\n", encoding="utf-8")


def _ensure_referenced_workspace_assets(env_dir: Path, task_dir: Path) -> None:
    pattern = re.compile(r"/workspace/assets/[A-Za-z0-9_./@+\\-]+")
    for script in task_dir.glob("*.sh"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.findall(text):
            local = env_dir / match[len("/workspace/"):]
            if not local.exists():
                _write_placeholder_asset(local)


def _patch_task_for_mock_verifier(env_dir: Path, task_dir: str, *, hook_timeout: Optional[int] = None) -> None:
    task_json = env_dir / "tasks" / task_dir / "task.json"
    if not task_json.exists():
        return
    data = json.loads(task_json.read_text(encoding="utf-8"))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for hook_name in ("pre_task", "post_task"):
        hook_path = hooks.get(hook_name)
        if isinstance(hook_path, str):
            local = _container_path_to_local(env_dir, hook_path)
            if local is not None:
                _ensure_noop_hook(local, hook_name)
    _ensure_referenced_workspace_assets(env_dir, task_json.parent)
    _patch_setup_scripts_for_smoke(task_json.parent)
    (task_json.parent / "mock_verifier.py").write_text(
        "def verify(*args, **kwargs):\n"
        "    return {'passed': False, 'score': 0, 'decided': True, 'feedback': 'mock verifier'}\n",
        encoding="utf-8",
    )
    hooks = data.setdefault("hooks", {})
    if isinstance(hooks, dict) and hook_timeout is not None:
        hooks["pre_task_timeout"] = int(hook_timeout)
    data["success"] = {
        "mode": "program",
        "spec": {"program": "mock_verifier.py::verify"},
    }
    task_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _patch_setup_scripts_for_smoke(task_dir: Path) -> None:
    setup = task_dir / "setup_task.sh"
    if not setup.exists():
        return
    text = setup.read_text(encoding="utf-8", errors="ignore")
    replacements = {
        'sudo -u ga python3 "$WORKSPACE_DIR/render.py"\n': 'sudo -u ga python3 "$WORKSPACE_DIR/render.py" || true\n',
        "import csv\nfrom datetime import datetime, timedelta\n": "import csv\nimport sys\nfrom datetime import datetime, timedelta\n",
        'wait_for_window "Visual Studio Code" 30\n': 'wait_for_window "Visual Studio Code" 90 || true\n',
        "wait_for_vscode 30\n": "wait_for_vscode 90 || true\n",
        "focus_vscode_window\n": "focus_vscode_window || true\n",
        'pkill -f "code" 2>/dev/null || true\nsleep 2\n': 'true # AGS smoke: skip broad VSCode pkill\nsleep 1\n',
        'focus_window "$WID"\n': 'focus_window "$WID" || true\n',
        'curl -L -s -o /tmp/source.mp4 "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"\n': (
            'curl -L --max-time 60 -s -o /tmp/source.mp4 '
            '"https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4" || '
            'ffmpeg -y -f lavfi -i testsrc=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=44100 '
            '-t 90 -c:v libx264 -preset ultrafast -c:a aac /tmp/source.mp4\n'
        ),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    setup.write_text(text, encoding="utf-8")


def _latest_summary(artifact_root: Path) -> tuple[Optional[Path], Optional[Dict[str, Any]]]:
    summaries = sorted(artifact_root.glob("episode_*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not summaries:
        return None, None
    path = summaries[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def _parse_log_timestamp(line: str) -> Optional[dt.datetime]:
    match = _LOG_TS_RE.match(line)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def _parse_stage_timings(log_text: str) -> Dict[str, float]:
    """Extract coarse reset-stage timings from run_single logs."""
    marks: Dict[str, dt.datetime] = {}
    create_start: Optional[dt.datetime] = None
    create_end: Optional[dt.datetime] = None
    for line in log_text.splitlines():
        timestamp = _parse_log_timestamp(line)
        if timestamp is None:
            continue
        if "Request POST https://api." in line and "/sandboxes" in line and create_start is None:
            create_start = timestamp
        elif "Response: 201 https://api." in line and "/sandboxes" in line and create_end is None:
            create_end = timestamp
        elif "Running post_start hook" in line:
            marks.setdefault("post_start_begin", timestamp)
        elif "Running pre_task hook" in line:
            marks.setdefault("pre_task_begin", timestamp)
        elif "Session:" in line:
            marks.setdefault("session_ready", timestamp)
        elif "Environment reset successfully" in line:
            marks.setdefault("reset_ok", timestamp)

    timings: Dict[str, float] = {}
    if create_start and create_end:
        timings["sandbox_create_sec"] = round((create_end - create_start).total_seconds(), 3)
    if "post_start_begin" in marks and "pre_task_begin" in marks:
        timings["post_start_sec"] = round((marks["pre_task_begin"] - marks["post_start_begin"]).total_seconds(), 3)
    reset_ready = marks.get("session_ready") or marks.get("reset_ok")
    if "pre_task_begin" in marks and reset_ready:
        timings["pre_task_sec"] = round((reset_ready - marks["pre_task_begin"]).total_seconds(), 3)
    if create_start and reset_ready:
        timings["reset_sec"] = round((reset_ready - create_start).total_seconds(), 3)
    return timings


def run_one(task: Dict[str, Any], args: argparse.Namespace, run_root: Path) -> Dict[str, Any]:
    safe_id = f"{task['env']}__{task['task_dir']}"
    result_path = run_root / "results" / f"{safe_id}.json"
    log_path = run_root / "logs" / f"{safe_id}.log"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists() and not args.force:
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except Exception:
            result_path.unlink()

    started = time.time()
    env_dir = prepare_task_env(
        task,
        run_root,
        args.template,
        hook_timeout=args.hook_timeout,
        force=args.force,
    )
    artifact_root = run_root / "artifacts" / safe_id
    cmd = [
        sys.executable,
        "-m",
        "agents.evaluation.run_single",
        "--env_dir",
        str(env_dir),
        "--task",
        task["task_dir"],
        "--steps",
        "1",
        "--agent",
        "MockDoneAgent",
        "--agent_args",
        "{}",
        "--setup_code",
        "none",
        "--cache_level",
        "pre_start",
        "--verifier_mode",
        args.verifier_mode,
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{REPO / 'src'}:{REPO}:{env.get('PYTHONPATH', '')}"
    env["GYM_ANYTHING_RUNNER"] = "ags"
    env["GYM_ANYTHING_AGS_TEMPLATE"] = args.template
    env.setdefault("GYM_ANYTHING_AGS_CREATE_RETRIES", str(args.create_retries))
    env.setdefault("GYM_ANYTHING_AGS_TIMEOUT", str(args.sandbox_timeout))
    env.setdefault("GYM_ANYTHING_POST_TASK_SETTLE_SEC", "0")
    if args.fast_post_start:
        env["GYM_ANYTHING_FAST_POST_START"] = "1"

    returncode = 1
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(REPO),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
            log.write(f"\nTIMEOUT after {args.timeout}s\n")

    summary_path, summary = _latest_summary(artifact_root)
    verifier = summary.get("verifier") if summary else None
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    stage_timings = _parse_stage_timings(log_text)
    hook_failures = []
    for marker in ("post_start hook failed", "pre_task hook failed"):
        if marker in log_text:
            hook_failures.append(marker)
    result = {
        **task,
        "env_dir": str(env_dir),
        "elapsed_sec": round(time.time() - started, 3),
        "worker_returncode": returncode,
        "log_path": str(log_path),
        "episode_summary": str(summary_path) if summary_path else None,
        "ok": returncode == 0 and verifier is not None and not hook_failures,
        "verifier": verifier,
        "hook_failures": hook_failures,
        "stage_timings": stage_timings,
        "error": None if returncode == 0 and not hook_failures else (
            "; ".join(hook_failures) if hook_failures else f"worker returned {returncode}"
        ),
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_summary(path: Path, run_root: Path, rows: List[Dict[str, Any]], started: float) -> None:
    by_env: Dict[str, Counter] = defaultdict(Counter)
    scores = Counter()
    returncodes = Counter()
    hook_failures = Counter()
    timing_values: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        env = row["env"]
        by_env[env]["total"] += 1
        if row.get("ok"):
            by_env[env]["ok"] += 1
        else:
            by_env[env]["failed"] += 1
        verifier = row.get("verifier") or {}
        if verifier:
            scores[str(verifier.get("score"))] += 1
        returncodes[str(row.get("worker_returncode"))] += 1
        for failure in row.get("hook_failures") or []:
            hook_failures[failure] += 1
        for key, value in (row.get("stage_timings") or {}).items():
            if isinstance(value, (int, float)):
                timing_values[key].append(float(value))
    timing_summary: Dict[str, Dict[str, float]] = {}
    for key, values in timing_values.items():
        ordered = sorted(values)
        if not ordered:
            continue
        timing_summary[key] = {
            "count": len(ordered),
            "p50": round(ordered[len(ordered) // 2], 3),
            "p90": round(ordered[max(0, int(len(ordered) * 0.90) - 1)], 3),
            "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
            "max": round(ordered[-1], 3),
        }
    summary = {
        "run_root": str(run_root),
        "completed": len(rows),
        "elapsed_sec": round(time.time() - started, 3),
        "by_env": {key: dict(value) for key, value in sorted(by_env.items())},
        "returncodes": dict(returncodes),
        "hook_failures": dict(hook_failures),
        "verifier_scores": dict(scores),
        "stage_timings": timing_summary,
    }
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_only_tasks(values: List[str]) -> set[tuple[str, str]]:
    selected: set[tuple[str, str]] = set()
    for value in values:
        path = Path(value)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if "ok" in row and row.get("ok"):
                    continue
                env = row.get("env") or row.get("env_name")
                task_dir = row.get("task_dir")
                task_id = row.get("task_id")
                if not task_dir and task_id:
                    task_dir = str(task_id).split("@", 1)[0]
                if env and task_dir:
                    selected.add((str(env), str(task_dir)))
            continue
        if "/" in value:
            env, task_dir = value.split("/", 1)
        elif ":" in value:
            env, task_dir = value.split(":", 1)
        else:
            raise SystemExit(f"Invalid --only value {value!r}; use env/task_dir, env:task_dir, or a JSONL file")
        selected.add((env, task_dir.split("@", 1)[0]))
    return selected


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--task-source", choices=("parquet", "registry"), default="parquet")
    parser.add_argument("--surface", choices=("raw", "verified", "ags_stable"), default="verified")
    parser.add_argument("--split", default="all")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--template", default=os.environ.get("GYM_ANYTHING_AGS_TEMPLATE") or os.environ.get("GYM_ANYTHING_AGS_TOOL_NAME"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--hook-timeout",
        type=int,
        default=None,
        help="Override task pre_task hook timeout in seconds for smoke runs; default keeps task/env defaults.",
    )
    parser.add_argument("--sandbox-timeout", type=int, default=3600)
    parser.add_argument("--create-retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--limit-per-env", type=int, default=None)
    parser.add_argument("--env", action="append", dest="envs", default=None)
    parser.add_argument("--exclude-env", action="append", dest="exclude_envs", default=None)
    parser.add_argument("--all-envs", action="store_true", help="Do not apply the default CUA package environment filter.")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run only env/task_dir entries. Accepts env/task_dir, env:task_dir, or a JSONL result/manifest file.",
    )
    parser.add_argument("--include-base", action="store_true")
    parser.add_argument("--verifier-mode", default="program", choices=("task", "program", "image_match", "multi", "vlm_checklist"))
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument("--list-only", action="store_true", help="Write the selected task manifest and exit without running AGS.")
    parser.add_argument(
        "--fast-post-start",
        action="store_true",
        help="Propagate GYM_ANYTHING_FAST_POST_START=1 into Linux hooks so supported envs skip GUI warm-up work.",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if not args.template:
        raise SystemExit("Set --template or GYM_ANYTHING_AGS_TEMPLATE to the AGS tool/template name")
    target_envs = set(args.envs) if args.envs else (None if args.all_envs else set(DEFAULT_TARGET_ENVS))
    if args.include_base and target_envs is not None:
        target_envs.add("libreoffice_base_env")

    only_tasks = _load_only_tasks(args.only) if args.only else None
    if args.task_source == "registry":
        tasks = load_registry_tasks(
            surface=args.surface,
            split=args.split,
            target_envs=target_envs,
            limit_per_env=args.limit_per_env,
            only_tasks=only_tasks,
        )
    else:
        tasks = load_tasks(
            args.data_root,
            target_envs=target_envs,
            limit_per_env=args.limit_per_env,
            only_tasks=only_tasks,
        )
    if args.exclude_envs:
        excluded = set(args.exclude_envs)
        tasks = [task for task in tasks if task["env"] not in excluded]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit(f"No matching tasks found in {args.data_root}")

    stamp = args.run_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = args.run_root / stamp
    run_root.mkdir(parents=True, exist_ok=True)
    write_jsonl(run_root / "manifest.jsonl", tasks)
    if args.list_only:
        summary = {
            "run_root": str(run_root),
            "task_source": args.task_source,
            "surface": args.surface,
            "split": args.split,
            "tasks": len(tasks),
            "by_env": dict(sorted(Counter(task["env"] for task in tasks).items())),
            "exclude_envs": sorted(args.exclude_envs or []),
        }
        (run_root / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    completed: List[Dict[str, Any]] = []
    summary_path = run_root / "summary.json"
    results_jsonl = run_root / "results.jsonl"
    started = time.time()
    print(f"run_root={run_root}")
    print(f"tasks={len(tasks)} concurrency={args.concurrency} template={args.template}")
    sys.stdout.flush()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_task = {
            executor.submit(run_one, task, args, run_root): task
            for task in tasks
        }
        for idx, future in enumerate(concurrent.futures.as_completed(future_to_task), 1):
            task = future_to_task[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {**task, "ok": False, "error": repr(exc), "worker_returncode": None, "verifier": None}
            completed.append(row)
            verifier = row.get("verifier") or {}
            print(
                f"[{idx}/{len(tasks)}] {task['env']}/{task['task_dir']} "
                f"ok={row.get('ok')} rc={row.get('worker_returncode')} score={verifier.get('score')}"
            )
            sys.stdout.flush()
            if idx % args.flush_every == 0 or idx == len(tasks):
                write_jsonl(results_jsonl, completed)
                write_summary(summary_path, run_root, completed, started)

    write_jsonl(results_jsonl, completed)
    write_summary(summary_path, run_root, completed, started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
