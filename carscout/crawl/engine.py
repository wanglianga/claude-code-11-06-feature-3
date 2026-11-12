"""Crawl engine: one Scrapy spider per platform, deterministic page/detail state machine.

Semantics enforced here
-----------------------
* List and detail are always two separate requests (per requirement).
* A page result is classified:
    ok                -> items persisted, one detail request per new/changed item
    empty_confirmed   -> reachable platform with genuinely zero stock: page advances
    empty_unverified / blocked_signal / schema_drift / http_error
                      -> exponential backoff retry, counted as a platform failure
* Cursor (`crawl_cursors`) records the last *fully processed* list page and the
  last committed listing id. A run that is killed resumes from the first
  unprocessed page; already-ingested bodies are skipped by the Redis
  fingerprint, so it does not re-scan from the top.
* Detail failures never block the cursor: the list observation is already
  persisted, and the failure is archived + classified per listing.
* Consecutive platform failures reaching FAILURE_THRESHOLD open the circuit
  (`platform_health.circuit_state`), stop only this platform, and keep all
  collected data. Other platforms are independent processes and unaffected.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from typing import Any, Iterable

import scrapy
from scrapy import signals

from carscout import matching, store
from carscout.config import settings
from carscout.crawl import common
from carscout.crawl.handlers import HANDLERS, DetailOutcome, ListPage, list_fingerprint
from carscout.db import pool
from carscout.crawl.common import archive_and_log
from carscout.ingest import circuit_open, record_anomaly, register_failure, register_success
from carscout.parsers import Anomaly

log = logging.getLogger("carscout.crawl")

RETRYABLE_DETAIL = {"blocked_signal", "signature_expired", "http_error", "empty_unverified",
                    "parser_error", "schema_drift"}
BLOCKING_DETAIL = {"blocked_signal", "signature_expired", "http_error"}


class PlatformSpider(scrapy.Spider):
    name = "carscout_platform"

    def __init__(self, platform: str, run_id: str, day: str, mode: str = "incremental",
                 resume: bool = False, start_page: int = 1, scope: str = "all", **kw: Any):
        super().__init__(**kw)
        self.platform = platform.upper()
        self.run_id = run_id
        self.day = day
        self.mode = mode
        self.scope = scope
        self.handler = HANDLERS[self.platform]
        self.resume = resume
        self.start_page = int(start_page)
        self.run_date = date.fromisoformat(day)

        self.stats: dict[str, int] = defaultdict(int)
        self.list_exhausted = False
        self.ended_naturally = False     # the sweep reached the platform's last page
        self.pending_details: dict[str, str] = {}         # pid -> page
        self.page_pending: dict[int, int] = defaultdict(int)
        self.completed_pages: set[int] = set()
        self.last_page_done = self.start_page - 1
        self.seen_pids: set[str] = set()
        self.items_by_pid: dict[str, dict[str, Any]] = {}
        self.stopped = False
        self.stop_reason: str | None = None
        self.stop_detail: str | None = None
        self.finished_status: str | None = None
        self.run_started: datetime = datetime.now(timezone.utc)
        self.consecutive_detail_blocked = 0
        self.consecutive_schema_drift = 0

        from carscout.crawl.handlers import platform_rate
        self.concurrent, self.base_delay = platform_rate(self.platform)

    # ------------------------------------------------------------------
    def start_requests(self):
        self.run_started = datetime.now(timezone.utc)
        if self.resume:
            cur = common.get_cursor(self.platform, self.scope) or {}
            c = cur.get("cursor") or {}
            self.start_page = max(self.start_page, int(c.get("last_completed_page") or 0) + 1)
        self.stats["start_page"] = self.start_page
        # register the cursor immediately so a hard kill is unambiguous: the row
        # exists, completed=false, and last_completed_page is the last safe point
        common.save_cursor(self.platform, self.scope,
                           {"last_completed_page": self.start_page - 1, "run_date": self.day},
                           completed=False, run_id=self.run_id, run_date=self.run_date,
                           items_seen=0)
        yield self._list_request(self.start_page, attempt=0)

    def _list_request(self, page: int, *, attempt: int) -> scrapy.Request:
        req = self.handler.list_request(page, self.day, attempt=attempt, delay=0.0)
        # retries must bypass the URL dupefilter: same URL, different attempt
        req.dont_filter = True
        req.meta["spider_page"] = page
        req.meta["attempt"] = attempt
        req.callback = self.on_list
        req.errback = self.on_transport_error
        if self.handler.needs_playwright:
            req.meta["playwright_context"] = f"{self.platform}-{self.run_id[:6]}"
        return req

    # -- list stage -----------------------------------------------------
    def on_list(self, response):
        page: int = int(response.meta.get("spider_page", 1))
        attempt: int = int(response.meta.get("attempt", 0))
        try:
            lp: ListPage = self.handler.parse_list(response, page=page, day=self.day)
            cat = lp.category
        except Exception as exc:  # noqa: BLE001
            lp = ListPage(anomaly=Anomaly("parser_error", {"error": repr(exc)}), raw=response.text)
            cat = "parser_error"

        archive_and_log(self.run_id, self.platform, response.url, lp.kind, lp.raw,
                        response.status if isinstance(response.status, int) else 200,
                        cat if cat != "ok" else "ok", lp.content_type,
                        extra={"stage": "list", "page": page, "items": len(lp.items),
                               "has_more": lp.has_more, "total": lp.total,
                               "anomaly": lp.anomaly.detail if lp.anomaly else None})

        if cat == "ok":
            register_success(self.platform)
            self.stats["list_pages_ok"] += 1
            self.stats["list_items"] += len(lp.items)
            self.page_pending[page] = 0
            if not lp.items and lp.total not in (None, 0):
                # declared total but no rows on this page: schema drift, not "no stock"
                cat = "schema_drift"
                lp.anomaly = Anomaly("schema_drift", {"reason": "declared total but empty page",
                                                      "total": lp.total})
            else:
                reqs = self._handle_items(lp.items, page=page)
                for req in reqs:
                    yield req
                if not lp.items:
                    self._page_done(page)
                if lp.has_more and self.stats["list_pages_ok"] < settings.max_pages_per_run:
                    self.stats["list_pages_total"] = self.stats["list_pages_ok"] + 1
                    yield self._list_request(page + 1, attempt=0)
                elif lp.has_more:
                    # page budget exhausted: this run is a partial sweep. The cursor
                    # holds last_completed_page, so the next/resumed run continues
                    # from the next page instead of rescanning from page 1.
                    # Finish only after the page's details drain, otherwise the
                    # page would never be committed and a resume would redo it.
                    self.stats["page_budget_exhausted"] = self.stats.get("page_budget_exhausted", 0) + 1
                    self.list_exhausted = True
                    self._maybe_finalize()
                else:
                    self.ended_naturally = True
                    self.list_exhausted = True
                    self._maybe_finalize()
                return

        if cat == "empty_confirmed":
            register_success(self.platform)
            self.stats["pages_empty_confirmed"] += 1
            self.page_pending[page] = 0
            self._page_done(page)
            self.ended_naturally = True
            self.list_exhausted = True
            self._maybe_finalize()
            return

        # anomaly -------------------------------------------------------
        self.stats[f"page_{cat}"] += 1
        retryable = cat in {"empty_unverified", "blocked_signal", "signature_expired", "http_error",
                            "schema_drift", "parser_error"}
        counts_toward_breaker = cat in {"empty_unverified", "blocked_signal", "http_error"}
        if counts_toward_breaker:
            health = common.failure_policy(self.platform, cat, detail=_detail_str(lp.anomaly), attempt=attempt)
        else:
            health = {"circuit_open": False, "backoff": min(settings.retry_max_delay,
                                                            settings.retry_base_delay * (2 ** attempt))}
            if cat == "schema_drift":
                self.consecutive_schema_drift += 1
        if health.get("circuit_open"):
            self._stop("circuit_open", f"page {page}: {cat} tripped the circuit breaker")
            return
        if cat == "schema_drift" and self.consecutive_schema_drift >= settings.failure_threshold:
            self._stop("schema_drift_threshold",
                       f"{self.consecutive_schema_drift} consecutive schema drift pages: probable redesign")
            return
        if retryable and attempt < settings.max_retries:
            self.stats["page_retries"] += 1
            time.sleep(float(health.get("backoff") or 1.0))
            yield self._list_request(page, attempt=attempt + 1)
            return
        # exhausted: this page is treated as failed; stop the run but keep data
        self._stop("page_failed", f"page {page} failed ({cat}) after {attempt} retries")

    # -- item fan-out ---------------------------------------------------
    def _handle_items(self, items: list[dict[str, Any]], *, page: int) -> list[scrapy.Request]:
        """Register every item of a page (eagerly) and return the detail requests.

        Bookkeeping must be complete before any detail can resolve, otherwise a
        fast detail could mark the page done while siblings are still queued.
        """
        requests: list[scrapy.Request] = []
        skipped: list[str] = []
        for item in items:
            pid = item["platform_listing_id"]
            if pid in self.seen_pids:
                continue
            self.seen_pids.add(pid)
            self.items_by_pid[pid] = item
            store.touch_list_only(item, run_id=self.run_id)
            self.page_pending[page] += 1
            self.pending_details[pid] = page
            fp = list_fingerprint(item)
            row = pool.query_one("SELECT detail_status FROM listings WHERE platform = %s "
                                 "AND platform_listing_id = %s", (self.platform, pid)) or {}
            force = self.mode in {"rerun"} or (
                self.mode == "resume" and row.get("detail_status") not in ("ok", "skipped_unchanged"))
            if (settings.fingerprint_skip_unchanged
                    and row.get("detail_status") in ("ok", "skipped_unchanged")
                    and common.seen_unchanged(self.platform, pid, fp)
                    and not force):
                self.stats["details_skipped_unchanged"] += 1
                store.mark_detail_problem(self.platform, pid, "skipped_unchanged")
                skipped.append(pid)
                continue
            req = self.handler.detail_request(item, self.day, attempt=0, delay=0.0)
            req.meta["spider_pid"] = pid
            req.meta["attempt"] = 0
            req.meta["spider_page"] = page
            req.callback = self.on_detail
            req.errback = self.on_transport_error
            if self.handler.needs_playwright:
                req.meta["playwright_context"] = f"{self.platform}-{self.run_id[:6]}"
            requests.append(req)
        for pid in skipped:
            self._resolve_detail(pid, page)
        return requests

    # -- detail stage ---------------------------------------------------
    def on_detail(self, response):
        pid: str | None = response.meta.get("spider_pid")
        attempt: int = int(response.meta.get("attempt", 0))
        page: int = int(response.meta.get("spider_page", 1))
        if not pid:
            return
        item = self.items_by_pid.get(pid, {})
        try:
            out: DetailOutcome = self.handler.parse_detail(response)
            cat = out.category
        except Exception as exc:  # noqa: BLE001
            out = DetailOutcome(anomaly=Anomaly("parser_error", {"error": repr(exc)}), raw=response.text)
            cat = "parser_error"

        archive_and_log(self.run_id, self.platform, response.url, out.kind, out.raw,
                        response.status if isinstance(response.status, int) else 200,
                        cat if cat != "ok" else "ok", out.content_type,
                        extra={"stage": "detail", "pid": pid,
                               "anomaly": out.anomaly.detail if out.anomaly else None})

        if cat == "missing":
            self.stats["detail_missing"] += 1
            store.mark_detail_problem(self.platform, pid, "missing")
            self._resolve_detail(pid, page)
            return

        if cat == "ok" and out.parsed is not None and out.parsed.missing_required():
            cat = "schema_drift"
            out.anomaly = Anomaly("schema_drift", {"missing": out.parsed.missing_required()})

        if cat == "ok" and out.parsed is not None:
            p = out.parsed
            row = store.upsert_listing(p, run_id=self.run_id, detail_status="ok",
                                       list_fields={"model": p.model})
            self.stats["detail_ok"] += 1
            self.consecutive_detail_blocked = 0
            self.consecutive_schema_drift = 0
            register_success(self.platform)
            common.remember_fingerprint(self.platform, pid, list_fingerprint(item))
            _enrich_images(row, p.image_urls)
            self._resolve_detail(pid, page)
            return

        self.stats[f"detail_{cat}"] += 1
        if cat in BLOCKING_DETAIL:
            self.consecutive_detail_blocked += 1
            health = common.failure_policy(self.platform, cat, detail=_detail_str(out.anomaly),
                                           attempt=attempt)
        else:
            health = {"circuit_open": False,
                      "backoff": min(settings.retry_max_delay,
                                     settings.retry_base_delay * (2 ** attempt))}
            if cat == "schema_drift":
                self.consecutive_schema_drift += 1
        if health.get("circuit_open"):
            store.mark_detail_problem(self.platform, pid, "blocked")
            self._resolve_detail(pid, page)
            self._stop("circuit_open", f"detail {cat} tripped the circuit breaker")
            return
        if cat == "schema_drift" and self.consecutive_schema_drift >= settings.failure_threshold:
            store.mark_detail_problem(self.platform, pid, "schema_drift")
            self._resolve_detail(pid, page)
            self._stop("schema_drift_threshold", "many detail pages drifted from schema")
            return
        if attempt < settings.max_retries:
            self.stats["detail_retries"] += 1
            time.sleep(float(health.get("backoff") or 1.0))
            yield self._retry_detail(pid, attempt + 1, page)
            return
        status = "blocked" if cat in BLOCKING_DETAIL or cat == "empty_unverified" else (
            "schema_drift" if cat == "schema_drift" else "missing")
        store.mark_detail_problem(self.platform, pid, status if status in
                                  ("blocked", "schema_drift", "missing") else "blocked")
        self._resolve_detail(pid, page)

    def _retry_detail(self, pid: str, attempt: int, page: int) -> scrapy.Request:
        item = self.items_by_pid[pid]
        req = self.handler.detail_request(item, self.day, attempt=attempt, delay=0.0)
        req.dont_filter = True
        req.meta["spider_pid"] = pid
        req.meta["attempt"] = attempt
        req.meta["spider_page"] = page
        req.callback = self.on_detail
        req.errback = self.on_transport_error
        if self.handler.needs_playwright:
            req.meta["playwright_context"] = f"{self.platform}-{self.run_id[:6]}"
        return req

    def on_transport_error(self, failure):
        req = failure.request
        pid = req.meta.get("spider_pid")
        page = req.meta.get("spider_page")
        attempt = int(req.meta.get("attempt", 0))
        err = repr(failure.value)
        record_anomaly(self.run_id, self.platform, "http_error", url=req.url,
                       detail={"error": err, "pid": pid, "page": page})
        health = common.failure_policy(self.platform, "http_error", detail=err, attempt=attempt)
        self.stats["transport_errors"] += 1
        if health.get("circuit_open"):
            if pid:
                store.mark_detail_problem(self.platform, pid, "blocked")
                self._resolve_detail(pid, int(page or 1))
            self._stop("circuit_open", "transport errors tripped the circuit breaker")
            return
        if attempt < settings.max_retries:
            if pid:
                yield self._retry_detail(pid, attempt + 1, int(page or 1))
            else:
                yield self._list_request(int(page or 1), attempt=attempt + 1)
            return
        if pid:
            store.mark_detail_problem(self.platform, pid, "blocked")
            self._resolve_detail(pid, int(page or 1))
        else:
            self._stop("page_failed", f"page {page} transport error")

    # -- completion tracking -------------------------------------------
    def _resolve_detail(self, pid: str, page: int) -> None:
        if pid not in self.pending_details:
            return
        self.pending_details.pop(pid, None)
        self.page_pending[page] = max(0, self.page_pending.get(page, 0) - 1)
        if self.page_pending[page] == 0:
            self._page_done(page)
        self._maybe_finalize()

    def _page_done(self, page: int) -> None:
        if page in self.completed_pages:
            return
        self.completed_pages.add(page)
        self.stats["pages_completed"] += 1
        advanced = False
        while (self.last_page_done + 1) in self.completed_pages:
            self.last_page_done += 1
            advanced = True
        if advanced:
            common.save_cursor(self.platform, self.scope,
                               {"last_completed_page": self.last_page_done,
                                "run_date": self.day},
                               completed=False, run_id=self.run_id, run_date=self.run_date,
                               items_seen=len(self.seen_pids), ok_pages_delta=1)

    def _maybe_finalize(self) -> None:
        if self.stopped or self.finished_status:
            return
        if not self.list_exhausted or self.pending_details:
            return
        self._finish("finished")

    # -- stop / finish --------------------------------------------------
    def _stop(self, reason: str, detail: str) -> None:
        if self.stopped or self.finished_status:
            return
        self.stopped = True
        self.stop_reason = reason
        self.stop_detail = detail
        self.stats[f"stop_{reason}"] += 1
        common.save_cursor(self.platform, self.scope,
                           {"last_completed_page": self.last_page_done, "run_date": self.day},
                           completed=False, run_id=self.run_id, run_date=self.run_date,
                           items_seen=len(self.seen_pids),
                           last_error={"reason": reason, "detail": detail},
                           stopped_reason=reason)
        self._finish("aborted")

    def _finish(self, status: str) -> None:
        if self.finished_status:
            return
        self.finished_status = status
        cursor = {"last_completed_page": self.last_page_done, "run_date": self.day}
        # cursor is only "completed" when the sweep actually reached the last page
        completed = status == "finished" and self.ended_naturally
        stats = {**dict(self.stats), "items_seen": len(self.seen_pids), "mode": self.mode,
                 "pending_details": len(self.pending_details), "circuit_state":
                     (circuit_open(self.platform)[1] or {}).get("circuit_state")}
        common.finish_run(self.run_id, status, stats=stats, cursor_after=cursor,
                          error=self.stop_detail if status != "finished" else None)
        common.save_cursor(self.platform, self.scope, cursor, completed=completed,
                           run_id=self.run_id, run_date=self.run_date,
                           items_seen=len(self.seen_pids), stopped_reason=self.stop_reason)
        if status == "finished" and completed:
            register_success(self.platform)
            if self.start_page == 1 and self.run_date == date.today():
                try:
                    n = store.deactivate_unseen(self.platform, self.run_started)
                    if n:
                        stats["deactivated"] = n
                        common.update_run_stats(self.run_id, deactivated=n)
                except Exception as exc:  # pragma: no cover
                    log.warning("deactivation failed: %s", exc)
        log.info("run %s platform %s finished with status=%s stats=%s", self.run_id,
                 self.platform, status, stats)

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._on_spider_closed, signal=signals.spider_closed)
        return spider

    def _on_spider_closed(self, spider=None, reason=None):
        if self.finished_status is None:
            self.stop_reason = self.stop_reason or "spider_closed"
            self.stop_detail = self.stop_detail or f"closed: {reason}"
            try:
                self._finish("aborted")
            except Exception:  # pragma: no cover
                pass


def _detail_str(an: Anomaly | None) -> str:
    return json.dumps(an.detail if an else {}, ensure_ascii=False, default=str)


def _enrich_images(row: dict[str, Any], urls: list[str]) -> None:
    from carscout.images import phash_remote

    hashes = []
    for url in [u for u in urls if u][:3]:
        meta = phash_remote(url)
        if meta:
            hashes.append((url, meta[0], meta[1], meta[2]))
    if hashes:
        store.save_image_hashes(int(row["id"]), hashes)
        store.set_primary_phash(int(row["id"]), hashes[0][1])
