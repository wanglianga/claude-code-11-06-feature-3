"""CarScout CLI entrypoint (`carscout`).

Commands
--------
  serve        run the HTTP API (ucjson)                 -> container default
  scheduler    resident scheduling loop (per platform frequency)
  fixture      start the fake four-platform server
  run          crawl now, optional platform / date / resume / rerun
  merge        run the cross-platform merge pass (or re-cluster from scratch)
  review       export / import the grey-zone review CSV
  export       series price distribution (json or csv)
  status       health & cursor summary
  archive-grep offline replay search over archived raw responses
  seed-platforms / init-db   bootstrap helpers
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date

import click

from carscout.config import settings

log = logging.getLogger("carscout.cli")


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def emit(obj) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, indent=1, default=_json_default))


@click.group()
@click.option("--log-level", default="INFO", envvar="LOG_LEVEL")
def cli(log_level: str) -> None:
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


# ---------------------------------------------------------------------------
@cli.command()
@click.option("--host", default=None)
@click.option("--port", type=int, default=None)
def serve(host: str | None, port: int | None) -> None:
    """Start the HTTP API (and bootstrap the schema)."""
    import uvicorn

    uvicorn.run("carscout.api.app:app", host=host or settings.api_host,
                port=port or settings.api_port, log_level="info")


@cli.command()
def scheduler() -> None:
    """Resident scheduler: crawls each platform on its own interval."""
    from carscout.scheduler import main_loop

    main_loop()


@cli.command()
@click.option("--port", type=int, default=8088)
@click.option("--host", default="0.0.0.0")
def fixture(port: int, host: str) -> None:
    """Run the fake multi-platform server used for local integration tests."""
    from carscout.fixture.server import make_server, RUNTIME

    srv = make_server(port, host)
    click.echo(f"fixture server on http://{host}:{port} (snapshot {RUNTIME.snapshot_day})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


@cli.command(name="init-db")
def init_db() -> None:
    """Create tables / seed platforms. Idempotent."""
    from carscout.db.pool import init_schema, healthcheck

    init_schema()
    emit({"ok": True, "dependencies": healthcheck()})


# ---------------------------------------------------------------------------
@cli.command(name="run")
@click.option("--platform", "-p", multiple=True,
              help="A/B/C/D (repeatable). Default: all")
@click.option("--date", "day", default=None, help="Snapshot date YYYY-MM-DD (default: today)")
@click.option("--mode", type=click.Choice(["incremental", "rerun", "resume"]), default="incremental")
@click.option("--resume", is_flag=True, help="Continue from the last committed page/listing")
@click.option("--no-merge", is_flag=True, help="Skip the merge pass after crawling")
@click.option("--max-parallel", type=int, default=None)
def run(platform, day, mode, resume, no_merge, max_parallel) -> None:
    """Crawl one or more platforms now (each in an isolated worker process)."""
    from carscout.crawl.runner import gc_stale_runs, run_platforms
    from carscout.db.pool import init_schema

    init_schema()
    gc_stale_runs()
    platforms = [p.upper() for p in platform] or None
    summary = run_platforms(platforms, day=day, mode=mode, resume=resume or mode == "resume",
                            auto_merge=not no_merge, max_parallel=max_parallel)
    emit(summary)
    failed = [p for p, s in summary.get("platforms", {}).items() if s.get("status") not in ("finished",)]
    skipped = [p for p, s in summary.get("platforms", {}).items() if s.get("status") is None]
    if summary.get("merge", {}).get("error"):
        sys.exit(2)
    if failed:
        # a platform that tripped its breaker is reported, not fatal for the others
        sys.exit(1)


@cli.command(name="merge")
@click.option("--rebuild", is_flag=True, help="Drop vehicles and re-cluster every active listing")
@click.option("--limit", type=int, default=2000)
def merge(rebuild: bool, limit: int) -> None:
    """Run or rebuild the cross-platform merge. Grey-zone pairs go to the review queue."""
    from carscout.db.pool import init_schema
    from carscout import matching

    init_schema()
    if rebuild:
        emit(matching.rebuild_all_vehicles())
    else:
        emit(matching.merge_pending_listings(limit=limit))


# ---------------------------------------------------------------------------
@cli.command(name="review")
@click.option("--export", "do_export", is_flag=True, help="Write CSV of pending grey-zone pairs")
@click.option("--out", type=click.Path(dir_okay=False), default="review_queue.csv")
@click.option("--import-file", "import_file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--decide", nargs=2, type=(int, str), multiple=True, metavar="ID DECISION",
              help="Directly decide one candidate: `--decide 12 merge`")
@click.option("--reviewer", default="operator")
def review(do_export, out, import_file, decide, reviewer) -> None:
    """Human confirmation loop: export CSV, then re-upload it with decisions."""
    from carscout.api import export as ex
    from carscout.db.pool import init_schema
    from carscout import matching

    init_schema()
    if do_export:
        text = ex.export_review_csv(status="pending")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        click.echo(f"wrote {out} ({text.count(chr(10)) - 1} pending rows)")
    if import_file:
        text = open(import_file, encoding="utf-8-sig").read()
        emit(ex.import_review_csv(text, reviewer=reviewer))
    for cid, decision in decide:
        if decision not in ("merge", "split"):
            raise click.BadParameter("decision must be merge|split")
        emit(matching.confirm_review(cid, decision, reviewer=reviewer))
    if not do_export and not import_file and not decide:
        emit({"pending": ex.export_review_csv(status="pending").count("\n") - 1})


@cli.command(name="export")
@click.option("--series", default=None)
@click.option("--brand", default=None)
@click.option("--days", type=int, default=30)
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json")
@click.option("--out", type=click.Path(dir_okay=False), default=None)
def export_cmd(series, brand, days, fmt, out) -> None:
    """近 N 天按车系的挂牌与报价分布。"""
    from carscout.api import export as ex
    from carscout.db.pool import init_schema

    init_schema()
    if fmt == "csv":
        rows = ex.series_summary(series=series, days=days)
        import csv
        import io

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                           ["brand", "series", "platform", "active_listings", "vehicles",
                            "price_min", "price_max", "price_median", "price_spread",
                            "earliest_listed", "latest_listed"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        text = buf.getvalue()
        if out:
            open(out, "w", encoding="utf-8").write(text)
            click.echo(f"wrote {out}")
        else:
            click.echo(text)
        return
    payload = ex.series_price_distribution(series=series, brand=brand, days=days)
    if out:
        open(out, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False,
                                                          indent=1, default=_json_default))
        click.echo(f"wrote {out}")
    else:
        emit(payload)


# ---------------------------------------------------------------------------
@cli.group(name="rules")
def rules_group() -> None:
    """归并规则版本：配置化、离线回放审批、灰度放量、回滚、证据解释。"""


@rules_group.command(name="default-spec")
def rules_default_spec() -> None:
    """打印 v1 基线 spec 模板（修改后用 create 提交新版本）。"""
    from carscout.rules import default_spec

    emit(default_spec())


@rules_group.command(name="list")
def rules_list() -> None:
    """全部规则版本与放量比例。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    emit(rules.list_versions())


@rules_group.command(name="show")
@click.option("--version-id", type=int, required=True)
def rules_show(version_id: int) -> None:
    """单个规则版本详情（含 spec 与 rollouts）。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    row = rules.get_version(version_id)
    if not row:
        raise click.ClickException(f"rule version {version_id} not found")
    row = dict(row)
    row["rollouts"] = rules.get_rollout(version_id)
    emit(row)


@rules_group.command(name="create")
@click.option("--name", required=True)
@click.option("--spec-file", type=click.Path(exists=True, dir_okay=False), required=True,
              help="JSON 文件：完整规则 spec（可从 default-spec 模板修改）")
@click.option("--changelog", default=None)
@click.option("--created-by", default="operator")
def rules_create(name: str, spec_file: str, changelog: str | None, created_by: str) -> None:
    """创建规则版本草稿（阈值/权重/必要证据全部版本化）。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    spec = json.loads(open(spec_file, encoding="utf-8").read())
    try:
        emit(rules.create_version(spec, name=name, changelog=changelog, created_by=created_by))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="replay")
@click.option("--version-id", type=int, required=True)
@click.option("--requester", default="operator")
def rules_replay(version_id: int, requester: str) -> None:
    """在历史存量上离线回放，输出新增合并/拆散人工确认预估（只读）。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    try:
        emit(rules.replay_version(version_id, actor=requester))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="approve")
@click.option("--replay-id", type=int, required=True)
@click.option("--reject", "do_reject", is_flag=True, help="拒绝（默认审批通过）")
@click.option("--reviewer", default="operator")
@click.option("--note", default=None)
def rules_approve(replay_id: int, do_reject: bool, reviewer: str, note: str | None) -> None:
    """对超过门槛的回放做人工审批。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    try:
        emit(rules.decide_replay(replay_id, not do_reject, reviewer=reviewer, note=note))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="activate")
@click.option("--version-id", type=int, required=True)
@click.option("--actor", default="operator")
@click.option("--rollout", "rollouts", multiple=True, metavar="PLATFORM=PCT",
              help="初始放量，可重复：--rollout A=10 --rollout B=0")
def rules_activate(version_id: int, actor: str, rollouts: tuple[str, ...]) -> None:
    """激活版本：未回放或超门槛未审批会被拒绝。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    initial = _parse_rollout_kv(rollouts)
    try:
        emit(rules.activate_version(version_id, actor=actor, initial_rollout=initial))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="rollout")
@click.option("--version-id", type=int, required=True)
@click.option("--set", "rollouts", multiple=True, metavar="PLATFORM=PCT",
              help="按平台放量比例，可重复：--set A=10 --set *=50")
@click.option("--actor", default="operator")
def rules_rollout(version_id: int, rollouts: tuple[str, ...], actor: str) -> None:
    """调整灰度放量（哈希分桶，可随时调大/调小/归零）。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    try:
        emit(rules.set_rollout(version_id, _parse_rollout_kv(rollouts), actor=actor))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="rollback")
@click.option("--reason", default="")
@click.option("--actor", default="operator")
def rules_rollback(reason: str, actor: str) -> None:
    """回滚当前 active 版本；已合并记录转待复核，不自动拆回。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    try:
        emit(rules.rollback_active(actor=actor, reason=reason))
    except ValueError as exc:
        raise click.ClickException(str(exc))


@rules_group.command(name="rereview")
@click.option("--limit", type=int, default=100)
def rules_rereview(limit: int) -> None:
    """列出回滚后待复核的挂牌（保留原归并关系）。"""
    from carscout.db.pool import init_schema
    from carscout import rules

    init_schema()
    rows = rules.pending_rereview_listings(limit=limit)
    emit({"count": len(rows), "items": rows})


@rules_group.command(name="explain")
@click.option("--a", "listing_a", type=int, required=True, help="挂牌 id A")
@click.option("--b", "listing_b", type=int, required=True, help="挂牌 id B")
@click.option("--version-id", type=int, default=None, help="规则版本 id，缺省用 active")
def rules_explain(listing_a: int, listing_b: int, version_id: int | None) -> None:
    """证据级解释：为何判为同一台车、命中哪些证据、各自权重多少。"""
    from carscout.db.pool import init_schema
    from carscout import matching

    init_schema()
    try:
        emit(matching.explain_pair(listing_a, listing_b, version_id))
    except ValueError as exc:
        raise click.ClickException(str(exc))


def _parse_rollout_kv(pairs: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for kv in pairs:
        if "=" not in kv:
            raise click.BadParameter(f"放量参数应为 PLATFORM=PCT，收到 {kv!r}")
        plat, pct = kv.split("=", 1)
        out[plat.strip().upper()] = int(pct.strip())
    return out


# ---------------------------------------------------------------------------
@cli.command()
def status() -> None:
    """Platform health, cursor progress and inventory counts."""
    from carscout.api import export as ex
    from carscout.db.pool import init_schema

    init_schema()
    emit(ex.overview())


@cli.command(name="archive-grep")
@click.argument("pattern")
@click.option("--platform", "-p", default=None)
@click.option("--date", "day", default=None, help="YYYY-MM-DD (default: all archived days)")
@click.option("--limit", type=int, default=20)
def archive_grep(pattern: str, platform: str | None, day: str | None, limit: int) -> None:
    """Offline replay: search archived raw bodies for a substring/regex."""
    import gzip
    import re
    from pathlib import Path

    root = Path(settings.data_dir) / settings.archive_dir_name
    if not root.exists():
        raise click.ClickException(f"no archive at {root}")
    rx = re.compile(pattern, re.IGNORECASE)
    days = [day] if day else sorted([p.name for p in root.iterdir() if p.is_dir()])
    hits = 0
    for d in days:
        base = root / d
        if not base.exists():
            continue
        plat_dirs = [platform.upper()] if platform else [p.name for p in base.iterdir() if p.is_dir()]
        for plat in plat_dirs:
            pdir = base / plat
            if not pdir.exists():
                continue
            for f in sorted(pdir.rglob("*.gz")):
                try:
                    with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                except Exception:
                    continue
                if rx.search(body):
                    m = rx.search(body)
                    snippet = body[max(0, m.start() - 60): m.end() + 120].replace("\n", " ") if m else ""
                    click.echo(f"{f}\n    {snippet[:300]}")
                    hits += 1
                    if hits >= limit:
                        click.echo(f"... stopped at {limit} hits")
                        return
    click.echo(f"{hits} hit(s)")


@cli.command(name="replay")
@click.option("--platform", required=True)
@click.option("--date", "day", required=True)
def replay(platform: str, day: str) -> None:
    """Re-extract listings from archived list pages of a past run (offline)."""
    import gzip
    from pathlib import Path

    from carscout import parsers, store
    from carscout.crawl.handlers import HANDLERS
    from carscout.db.pool import init_schema

    init_schema()
    root = Path(settings.data_dir) / settings.archive_dir_name / day / platform.upper()
    if not root.exists():
        raise click.ClickException(f"nothing archived under {root}")
    handler = HANDLERS[platform.upper()]
    n = 0
    for run_dir in sorted(root.iterdir()):
        for f in sorted(run_dir.glob("list_*.gz")):
            meta = f.with_suffix("").with_suffix(".meta.json")
            # meta path: list_x.json.gz -> list_x.json.meta.json  OR  list_x.html.gz
            alt_meta = Path(str(f)[: -len(".gz")] + ".meta.json")
            body = gzip.open(f, "rt", encoding="utf-8", errors="replace").read()
            if body.lstrip().startswith("{"):
                payload = json.loads(body)
                items, an = (parsers.parse_b_list(payload, base_url=settings.fixture_base_url)
                             if platform.upper() == "B" else
                             parsers.decode_d_list(payload, base_url=settings.fixture_base_url))
            else:
                page = 1
                items, an = (parsers.parse_c_list(body, base_url=settings.fixture_base_url)
                             if platform.upper() == "C"
                             else parsers.parse_a_list(body, base_url=settings.fixture_base_url))
            for it in items:
                n += 1
            click.echo(f"{f.name}: {len(items)} item(s) anomaly={an}")
    click.echo(f"replayed {n} list rows (no dedup/re-fetch; use for parser checks)")


if __name__ == "__main__":
    cli()
