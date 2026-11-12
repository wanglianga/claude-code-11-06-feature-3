"""HTTP API (FastAPI). No admin UI: JSON endpoints + CSV for the human review loop."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from carscout import matching, rules
from carscout.api import export
from carscout.config import settings
from carscout.db import pool

app = FastAPI(
    title="CarScout 二手车多平台采集与归并服务",
    version="1.0.0",
    description=(
        "常驻采集服务：按平台维护抓取范围/频次/分页/增量游标，列表与详情两次请求，"
        "异常分类与熔断，跨平台归并（VIN 直判 + 特征召回打分 + 灰区人工确认），"
        "以及近 30 天按车系的挂牌/报价分布导出。"
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.init_schema()
    yield
    pool.close_pool()


app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------------
# health / overview
# ---------------------------------------------------------------------------
@app.get("/healthz", tags=["ops"], summary="存活与依赖探测")
def healthz() -> dict[str, Any]:
    checks = pool.healthcheck()
    ok = all(checks.values())
    return {"ok": ok, "dependencies": checks, "version": "1.0.0"}


@app.get("/api/overview", tags=["ops"], summary="采集健康总览（每平台游标、熔断状态、近 7 天异常）")
def overview() -> dict[str, Any]:
    return export.overview()


@app.get("/api/platforms", tags=["ops"])
def platforms() -> list[dict[str, Any]]:
    return pool.query("SELECT code, name, enabled, config FROM platforms ORDER BY code")


@app.get("/api/health/{platform}", tags=["ops"], summary="单平台熔断状态")
def platform_health(platform: str) -> dict[str, Any]:
    row = pool.query_one(
        """SELECT * FROM platform_health WHERE platform = %s""", (platform.upper(),))
    if not row:
        raise HTTPException(404, f"unknown platform {platform}")
    return row


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------
@app.get("/api/export/series", tags=["export"],
         summary="按车系返回近 30 天各平台的挂牌与报价分布")
def export_series(
    series: str | None = Query(None, description="车系，如 3系 / 凯美瑞"),
    brand: str | None = Query(None, description="品牌，如 宝马"),
    days: int = Query(30, ge=1, le=365, description="回溯窗口天数，默认 30"),
    platform: list[str] | None = Query(None, description="限定平台，可重复传参"),
) -> dict[str, Any]:
    return export.series_price_distribution(series=series, brand=brand, days=days, platforms=platform)


@app.get("/api/export/series/csv", tags=["export"], response_class=PlainTextResponse)
def export_series_csv(
    series: str | None = Query(None),
    brand: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
) -> Response:
    import csv
    import io

    rows = export.series_summary(series=series, days=days)
    buf = io.StringIO()
    fields = ["brand", "series", "platform", "active_listings", "vehicles", "price_min",
              "price_max", "price_median", "price_spread", "earliest_listed", "latest_listed"]
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({**r, "earliest_listed": r.get("earliest_listed"), "latest_listed": r.get("latest_listed")})
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="series_prices.csv"'})


@app.get("/api/vehicles", tags=["export"], summary="归并后的车源列表（可按车系/品牌过滤）")
def vehicles(
    series: str | None = None,
    brand: str | None = None,
    platform: str | None = None,
    active_only: bool = True,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = pool.query(
        """
        SELECT v.id, v.brand, v.series, v.reg_year, v.reg_month, v.mileage_km, v.city,
               v.platform_count, v.first_listed_at, v.last_seen_at,
               (SELECT count(*) FROM listings l WHERE l.vehicle_id = v.id AND l.is_active) AS active_listings,
               (SELECT min(price_yuan) FROM listings l WHERE l.vehicle_id = v.id AND l.is_active) AS price_min,
               (SELECT max(price_yuan) FROM listings l WHERE l.vehicle_id = v.id AND l.is_active) AS price_max,
               (SELECT count(*) FROM listing_price_events e JOIN listings l ON l.id = e.listing_id
                WHERE l.vehicle_id = v.id AND e.previous_price > e.price_yuan) AS price_cuts,
               EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id
                       AND l.match_status IN ('manual_pending', 'rejected', 'pending_rereview')) AS provisional
        FROM vehicles v
        WHERE (%s::text IS NULL OR v.series = %s::text)
          AND (%s::text IS NULL OR v.brand = %s::text)
          AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM listings l WHERE l.vehicle_id = v.id
                                           AND l.platform = %s::text))
        ORDER BY v.platform_count DESC, v.last_seen_at DESC
        LIMIT %s::int OFFSET %s::int
        """,
        (series, series, brand, brand, platform, platform, limit, offset))
    return {"count": len(rows), "items": rows}


@app.get("/api/vehicles/{vehicle_id}", tags=["export"],
         summary="单车档案：在几个平台挂过、各挂多少钱、降过几次价、首次挂牌距今多久")
def vehicle(vehicle_id: int) -> dict[str, Any]:
    dossier = export.vehicle_dossier(vehicle_id)
    if not dossier:
        raise HTTPException(404, "vehicle not found")
    return dossier


@app.get("/api/vehicles/{vehicle_id}/dossier.csv", response_class=PlainTextResponse, tags=["export"])
def vehicle_csv(vehicle_id: int) -> Response:
    dossier = export.vehicle_dossier(vehicle_id)
    if not dossier:
        raise HTTPException(404, "vehicle not found")
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["platform", "listing_id", "current_price", "listed_at", "active", "price_cuts",
                "first_seen_at", "removed_at", "url"])
    for l in dossier["listings"]:
        w.writerow([l["platform"], l["platform_listing_id"], l["price_yuan"], l["listed_at"],
                    l["is_active"], l["price_cuts"], l["first_seen_at"], l["removed_at"],
                    l["source_url"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="vehicle-{vehicle_id}.csv"'})


# ---------------------------------------------------------------------------
# review loop (CSV only, no UI)
# ---------------------------------------------------------------------------
@app.get("/api/review/queue", tags=["review"], summary="灰区人工确认队列（JSON 预览）")
def review_queue(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    rows = pool.query(
        """SELECT q.id, q.candidate_id, q.batch_id, q.status, c.score,
                  a.platform AS platform_a, a.platform_listing_id AS pid_a,
                  b.platform AS platform_b, b.platform_listing_id AS pid_b,
                  a.title AS title_a, b.title AS title_b
           FROM manual_review_queue q
           JOIN match_candidates c ON c.id = q.candidate_id
           JOIN listings a ON a.id = c.listing_a
           JOIN listings b ON b.id = c.listing_b
           WHERE q.status = 'pending'
           ORDER BY c.score DESC, q.id LIMIT %s""", (limit,))
    for r in rows:
        r["score"] = float(r["score"]) if r.get("score") is not None else None
    return {"count": len(rows), "items": rows}


@app.get("/api/review/export.csv", response_class=PlainTextResponse, tags=["review"],
         summary="导出灰区候选为 CSV（人工修改 decision 列后回传）")
def review_export(batch_id: str | None = None, min_score: float | None = None) -> Response:
    csv_text = export.export_review_csv(batch_id=batch_id, status="pending", min_score=min_score)
    return PlainTextResponse(csv_text, media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="review_queue.csv"'})


class ReviewImportBody(BaseModel):
    csv: str = Field(..., description="导出的 CSV 内容，decision 列填 merge 或 split")
    reviewer: str = "operator"
    learn_rules: bool = True


@app.post("/api/review/import", tags=["review"], summary="回传人工确认结果，沉淀为规则")
def review_import(body: ReviewImportBody) -> dict[str, Any]:
    return export.import_review_csv(body.csv, reviewer=body.reviewer, learn_rules=body.learn_rules)


@app.post("/api/review/import-file", tags=["review"],
          summary="以 multipart 上传 CSV 回传人工确认结果")
async def review_import_file(request: Request) -> dict[str, Any]:
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "multipart field 'file' required")
    content = await upload.read() if hasattr(upload, "read") else str(upload).encode()
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else str(content)
    reviewer = str(form.get("reviewer") or "operator")
    learn = str(form.get("learn_rules") or "true").lower() not in {"0", "false", "no"}
    return export.import_review_csv(text, reviewer=reviewer, learn_rules=learn)


class ConfirmBody(BaseModel):
    decision: str = Field(..., pattern="^(merge|split)$")
    reviewer: str = "operator"
    note: str | None = None
    learn_rule: bool = True


@app.post("/api/review/{candidate_id}/confirm", tags=["review"],
          summary="单条确认（等价于 CSV 回传中的一行）")
def review_confirm(candidate_id: int, body: ConfirmBody) -> dict[str, Any]:
    res = matching.confirm_review(candidate_id, body.decision, reviewer=body.reviewer,
                                  note=body.note, learn_rule=body.learn_rule)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "not found"))
    return res


@app.get("/api/rules", tags=["review"], summary="已沉淀的归并规则")
def list_match_rules() -> list[dict[str, Any]]:
    rows = pool.query("SELECT * FROM match_rules ORDER BY applied_count DESC, id")
    for r in rows:
        r["confidence"] = float(r["confidence"]) if r.get("confidence") is not None else None
    return rows


@app.get("/api/candidates/{candidate_id}", tags=["review"])
def candidate(candidate_id: int) -> dict[str, Any]:
    row = pool.query_one(
        """SELECT * FROM match_candidates WHERE id = %s""", (candidate_id,))
    if not row:
        raise HTTPException(404, "candidate not found")
    for k in ("score",):
        if row.get(k) is not None:
            row[k] = float(row[k])
    a = pool.query_one("SELECT * FROM listings WHERE id = %s", (row["listing_a"],))
    b = pool.query_one("SELECT * FROM listings WHERE id = %s", (row["listing_b"],))
    return {"candidate": row, "listing_a": a, "listing_b": b}


# ---------------------------------------------------------------------------
# versioned merge rules: 配置化 / 灰度 / 回放审批 / 回滚 / 证据解释
# ---------------------------------------------------------------------------
class RuleVersionBody(BaseModel):
    name: str = Field(..., description="规则版本名称")
    spec: dict[str, Any] = Field(..., description="完整规则参数（可先 GET default-spec 取模板）")
    changelog: str | None = None
    created_by: str = "operator"


class ActivateBody(BaseModel):
    actor: str = "operator"
    initial_rollout: dict[str, int] | None = Field(
        None, description="激活时的初始放量，如 {\"A\": 10}；缺省为 0%（不放量）")


class RolloutBody(BaseModel):
    rollouts: dict[str, int] = Field(..., description="平台 -> 放量百分比 0~100，键为 A/B/C/D 或 *")
    actor: str = "operator"


class RollbackBody(BaseModel):
    reason: str = ""
    actor: str = "operator"


class ReplayDecisionBody(BaseModel):
    approve: bool
    reviewer: str = "operator"
    note: str | None = None


@app.get("/api/rule-versions/default-spec", tags=["rules"],
         summary="规则 spec 模板（v1 基线参数，可在此基础上修改后提交新版本）")
def default_rule_spec() -> dict[str, Any]:
    return rules.default_spec()


@app.get("/api/rule-versions", tags=["rules"], summary="全部规则版本（含放量比例）")
def rule_versions() -> list[dict[str, Any]]:
    return rules.list_versions()


@app.post("/api/rule-versions", tags=["rules"], status_code=201,
          summary="创建规则版本草稿（阈值/权重/必要证据全部版本化管理）")
def create_rule_version(body: RuleVersionBody) -> dict[str, Any]:
    try:
        return rules.create_version(body.spec, name=body.name, changelog=body.changelog,
                                    created_by=body.created_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/rule-versions/{version_id}", tags=["rules"], summary="单个规则版本详情")
def rule_version(version_id: int) -> dict[str, Any]:
    row = rules.get_version(version_id)
    if not row:
        raise HTTPException(404, "rule version not found")
    row["rollouts"] = rules.get_rollout(version_id)
    return row


@app.post("/api/rule-versions/{version_id}/replay", tags=["rules"],
          summary="在历史存量上离线回放，预估新增合并/拆散人工确认数（只读，不改存量）")
def replay_rule_version(version_id: int, requester: str = "operator") -> dict[str, Any]:
    try:
        return rules.replay_version(version_id, actor=requester)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/replays", tags=["rules"], summary="离线回放记录与影响预估")
def replays(version_id: int | None = None) -> list[dict[str, Any]]:
    return rules.list_replays(version_id)


@app.get("/api/replays/{replay_id}", tags=["rules"])
def replay_detail(replay_id: int) -> dict[str, Any]:
    row = pool.query_one("SELECT * FROM merge_rule_replays WHERE id = %s", (replay_id,))
    if not row:
        raise HTTPException(404, "replay not found")
    return row


@app.post("/api/replays/{replay_id}/decision", tags=["rules"],
          summary="人工审批回放（超过影响门槛时激活前必须审批通过）")
def decide_replay(replay_id: int, body: ReplayDecisionBody) -> dict[str, Any]:
    try:
        return rules.decide_replay(replay_id, body.approve, reviewer=body.reviewer, note=body.note)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/rule-versions/{version_id}/activate", tags=["rules"],
          summary="激活规则版本；未回放或超门槛未审批将被拒绝")
def activate_rule_version(version_id: int, body: ActivateBody) -> dict[str, Any]:
    try:
        return rules.activate_version(version_id, actor=body.actor,
                                      initial_rollout=body.initial_rollout)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/rule-versions/{version_id}/rollout", tags=["rules"],
          summary="按平台灰度放量（哈希分桶，可随时调大/调小/归零）")
def set_rule_rollout(version_id: int, body: RolloutBody) -> dict[str, Any]:
    try:
        return rules.set_rollout(version_id, body.rollouts, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/rule-versions/{version_id}/rollback", tags=["rules"],
          summary="回滚：切回上一版本；新版本合并过的记录转待复核，绝不自动拆回")
def rollback_rule_version(version_id: int, body: RollbackBody) -> dict[str, Any]:
    active = rules.active_version_row()
    if not active or int(active["id"]) != version_id:
        raise HTTPException(400, "只能回滚当前 active 版本")
    try:
        return rules.rollback_active(actor=body.actor, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/pending-rereview", tags=["rules"],
         summary="回滚后被标记为待复核的挂牌（保留原归并关系，等待人工处理）")
def pending_rereview(limit: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    rows = rules.pending_rereview_listings(limit=limit)
    return {"count": len(rows), "items": rows}


@app.get("/api/explain", tags=["rules"],
         summary="证据级解释：两条车源为何被判为同一台车、命中哪些证据、各自权重")
def explain(listing_a: int = Query(..., description="挂牌 id A"),
            listing_b: int = Query(..., description="挂牌 id B"),
            rule_version_id: int | None = Query(None, description="缺省用当前 active 版本")) -> dict[str, Any]:
    try:
        return matching.explain_pair(listing_a, listing_b, rule_version_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------------------
# runs / archive
# ---------------------------------------------------------------------------
class RerunBody(BaseModel):
    day: str | None = Field(None, description="快照日期 YYYY-MM-DD，默认今天")
    mode: str = Field("incremental", pattern="^(incremental|rerun|resume)$")
    platforms: list[str] | None = Field(None, description="平台子集，默认全部")
    wait: bool = Field(False, description="true=等待完成并返回汇总；false=后台派发")


@app.post("/api/runs", tags=["ops"], summary="按平台 / 指定日期重跑（异步派发，也可 wait=true 等结果）")
def trigger_run(body: RerunBody) -> dict[str, Any]:
    from carscout.crawl.runner import start_platform

    plats = [p.upper() for p in (body.platforms or ["A", "B", "C", "D"])]
    day = body.day or date.today().isoformat()
    if body.wait:
        from carscout.crawl.runner import run_platforms
        return run_platforms(plats, day=day, mode=body.mode,
                             resume=body.mode == "resume", auto_merge=True)
    started = [start_platform(p, day=day, mode=body.mode, resume=body.mode == "resume")
               for p in plats]
    return {"day": day, "mode": body.mode, "started": started}


@app.get("/api/runs", tags=["ops"])
def runs(platform: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    rows = pool.query(
        """SELECT id, platform, scope, run_date, mode, status, started_at, finished_at, stats, error
           FROM crawl_runs
           WHERE (%s::text IS NULL OR platform = %s::text)
           ORDER BY started_at DESC LIMIT %s""",
        (platform.upper() if platform else None, platform.upper() if platform else None, limit))
    return {"count": len(rows), "items": rows}


@app.get("/api/anomalies", tags=["ops"], summary="失败原因分类（近 N 天）")
def anomalies(platform: str | None = None, days: int = Query(7, ge=1, le=90),
              limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    rows = pool.query(
        """SELECT id, platform, category, url, http_status, detail, created_at
           FROM ingest_anomalies
           WHERE created_at > now() - make_interval(days => %s)
             AND (%s::text IS NULL OR platform = %s::text)
           ORDER BY id DESC LIMIT %s""",
        (days, platform.upper() if platform else None, platform.upper() if platform else None, limit))
    return {"count": len(rows), "items": rows}


@app.get("/api/archive", tags=["ops"], summary="原始响应与 HTML 留档索引（可按平台/日期过滤）")
def archive(platform: str | None = None, day: date | None = None,
            limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    rows = pool.query(
        """SELECT id, run_id, platform, url, http_status, kind, category, path, size_bytes, created_at
           FROM ingest_archive
           WHERE (%s::text IS NULL OR platform = %s::text)
             AND (%s::date IS NULL OR created_at::date = %s::date)
           ORDER BY id DESC LIMIT %s""",
        (platform.upper() if platform else None, platform.upper() if platform else None,
         day, day, limit))
    return {"count": len(rows), "items": rows}
