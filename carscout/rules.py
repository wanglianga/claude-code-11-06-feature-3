"""Versioned merge-rule lifecycle.

A *rule version* is an immutable, audited snapshot of every knob the merge
engine uses: similarity thresholds, per-field weights, recall windows,
evidence gates and — critically — the **necessary evidence** that must be
present before a pair is allowed to auto-merge (e.g. a matching VIN).

Lifecycle:

    draft ──replay──▶ pending replay ──(within threshold / approved)──▶ active
                       │ exceeds threshold => human approval mandatory
    active ──rollback──▶ rollback（按新版本合并过的记录转 pending_rereview，
                         保留 vehicle_id，绝不自动拆回，历史统计不跳变）

Only one version is active at a time. An active version is exposed per
platform through percentage rollouts (stable hash bucket on the listing id),
so a new version can start at e.g. A=10% and be widened or rolled back.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from carscout.config import settings
from carscout.db import pool

BASELINE_VERSION = 1

# Evidence codes that may be declared as necessary conditions.
KNOWN_REQUIRED_EVIDENCE = ("vin_match", "image_agreement", "text_agreement")

_WEIGHT_KEYS = ("series", "reg_month", "mileage", "city", "transmission",
                "displacement", "transfer", "image", "text")


# ---------------------------------------------------------------------------
# RuleSpec
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleSpec:
    """Typed view over the JSONB `spec` column."""

    raw: dict[str, Any] = field(default_factory=dict)

    # -- thresholds ---------------------------------------------------------
    @property
    def auto_merge_score(self) -> float:
        return float(self.raw["thresholds"]["auto_merge"])

    @property
    def gray_zone_score(self) -> float:
        return float(self.raw["thresholds"]["gray_zone"])

    # -- weights ------------------------------------------------------------
    @property
    def weights(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.raw["weights"].items()}

    def weight(self, key: str) -> float:
        return float(self.raw["weights"].get(key, 0.0))

    # -- recall -------------------------------------------------------------
    @property
    def recall_reg_months(self) -> int:
        return int(self.raw["recall"]["reg_months"])

    @property
    def recall_mileage_km(self) -> int:
        return int(self.raw["recall"]["mileage_km"])

    @property
    def candidate_limit(self) -> int:
        return int(self.raw["recall"]["candidate_limit"])

    # -- gates --------------------------------------------------------------
    @property
    def image_gate_hamming(self) -> int:
        return int(self.raw["gates"]["image_hamming"])

    @property
    def text_gate_similarity(self) -> float:
        return float(self.raw["gates"]["text_similarity"])

    @property
    def gate_cap_offset(self) -> float:
        return float(self.raw["gates"]["cap_offset"])

    # -- necessary evidence -------------------------------------------------
    @property
    def required_evidence(self) -> tuple[str, ...]:
        return tuple(self.raw.get("required_evidence") or ())

    # -- scoring knobs ------------------------------------------------------
    @property
    def series_min_similarity(self) -> float:
        return float(self.raw["scoring"]["series_min"])

    @property
    def vin_conflict_penalty(self) -> float:
        return float(self.raw["scoring"]["vin_conflict_penalty"])

    @property
    def mileage_tolerance_km(self) -> float:
        return float(self.raw["scoring"]["mileage_tolerance_km"])

    @property
    def mileage_scale_km(self) -> float:
        return float(self.raw["scoring"]["mileage_scale_km"])

    @property
    def displacement_tolerance_ml(self) -> float:
        return float(self.raw["scoring"]["displacement_tolerance_ml"])

    @property
    def displacement_scale_ml(self) -> float:
        return float(self.raw["scoring"]["displacement_scale_ml"])

    @property
    def reg_decay_months(self) -> float:
        return float(self.raw["scoring"]["reg_decay_months"])

    @property
    def unknown_reg_credit(self) -> float:
        return float(self.raw["scoring"]["unknown_reg_credit"])

    @property
    def unknown_transmission_credit(self) -> float:
        return float(self.raw["scoring"]["unknown_transmission_credit"])

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.raw, ensure_ascii=False))


def default_spec() -> dict[str, Any]:
    """The v1 baseline: every value reproduces the original hardcoded engine."""
    return {
        "version_schema": 1,
        "thresholds": {"auto_merge": 0.82, "gray_zone": 0.60},
        "weights": {
            "series": 0.34, "reg_month": 0.16, "mileage": 0.14, "city": 0.08,
            "transmission": 0.04, "displacement": 0.03, "transfer": 0.03,
            "image": 0.30, "text": 0.26,
        },
        "recall": {"reg_months": 9, "mileage_km": 9000, "candidate_limit": 40},
        "gates": {"image_hamming": 12, "text_similarity": 0.25, "cap_offset": 0.17},
        # necessary conditions for auto-merge; v1 has none (gates only cap)
        "required_evidence": [],
        "scoring": {
            "series_min": 0.70, "vin_conflict_penalty": -0.10,
            "mileage_tolerance_km": 1500, "mileage_scale_km": 12000,
            "displacement_tolerance_ml": 50, "displacement_scale_ml": 400,
            "reg_decay_months": 10, "unknown_reg_credit": 0.04,
            "unknown_transmission_credit": 0.01,
        },
    }


def validate_spec(spec: dict[str, Any]) -> None:
    """Raise ValueError with a Chinese message on any malformed rule spec."""
    if not isinstance(spec, dict):
        raise ValueError("规则 spec 必须是 JSON 对象")
    try:
        th = spec["thresholds"]
        auto, gray = float(th["auto_merge"]), float(th["gray_zone"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("thresholds.auto_merge / gray_zone 必须是 0~1 的小数") from exc
    if not (0.0 < gray < auto <= 1.0):
        raise ValueError("阈值必须满足 0 < gray_zone < auto_merge <= 1")
    weights = spec.get("weights") or {}
    if not isinstance(weights, dict) or any(k not in weights for k in _WEIGHT_KEYS):
        raise ValueError(f"weights 必须包含全部字段权重：{', '.join(_WEIGHT_KEYS)}")
    for k, v in weights.items():
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"权重 {k} 必须是非负数字")
    recall = spec.get("recall") or {}
    for k in ("reg_months", "mileage_km", "candidate_limit"):
        if not isinstance(recall.get(k), int) or recall[k] <= 0:
            raise ValueError(f"recall.{k} 必须是正整数")
    gates = spec.get("gates") or {}
    if not isinstance(gates.get("image_hamming"), int) or not 0 <= gates["image_hamming"] <= 64:
        raise ValueError("gates.image_hamming 必须是 0~64 的整数")
    if not isinstance(gates.get("text_similarity"), (int, float)) or not 0 <= gates["text_similarity"] <= 1:
        raise ValueError("gates.text_similarity 必须是 0~1 的小数")
    if not isinstance(gates.get("cap_offset"), (int, float)) or gates["cap_offset"] < 0:
        raise ValueError("gates.cap_offset 必须是非负数字")
    required = spec.get("required_evidence") or []
    if not isinstance(required, list) or any(r not in KNOWN_REQUIRED_EVIDENCE for r in required):
        raise ValueError(
            f"required_evidence 只能取 {', '.join(KNOWN_REQUIRED_EVIDENCE)}")
    scoring = spec.get("scoring") or {}
    for k in ("series_min", "vin_conflict_penalty", "mileage_tolerance_km", "mileage_scale_km",
              "displacement_tolerance_ml", "displacement_scale_ml", "reg_decay_months",
              "unknown_reg_credit", "unknown_transmission_credit"):
        if not isinstance(scoring.get(k), (int, float)):
            raise ValueError(f"scoring.{k} 必须是数字")


# ---------------------------------------------------------------------------
# version CRUD
# ---------------------------------------------------------------------------
def ensure_baseline_version() -> int:
    """Idempotently create the v1 baseline (active, fully rolled out)."""
    with pool.tx() as conn:
        cur = conn.execute("SELECT id FROM merge_rule_versions WHERE version = %s",
                           (BASELINE_VERSION,))
        row = cur.fetchone()
        cur.close()
        if row:
            return int(row["id"])
        cur = conn.execute(
            """INSERT INTO merge_rule_versions
               (version, name, status, spec, changelog, created_by, approved_by, activated_at)
               VALUES (%s, %s, 'active', %s::jsonb, %s, 'system', 'system', now())
               RETURNING id""",
            (BASELINE_VERSION, "v1 基线规则（硬编码参数迁移）",
             json.dumps(default_spec(), ensure_ascii=False),
             "迁移自 score_pair 原始硬编码阈值与权重，行为完全等价"),
        )
        vid = int(cur.fetchone()["id"])
        cur.close()
        return vid


def list_versions() -> list[dict[str, Any]]:
    rows = pool.query(
        """SELECT v.*,
                  (SELECT jsonb_object_agg(platform, rollout_pct)
                     FROM merge_rule_rollouts r WHERE r.rule_version_id = v.id) AS rollouts
           FROM merge_rule_versions v ORDER BY version DESC""")
    for r in rows:
        r["rollouts"] = r.get("rollouts") or {}
    return rows


def get_version(version_id: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM merge_rule_versions WHERE id = %s", (version_id,))


def get_version_by_number(version: int) -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM merge_rule_versions WHERE version = %s", (version,))


def load_spec(version_id: int | None) -> RuleSpec:
    """Resolve a version id to its RuleSpec; fall back to the active version."""
    row = None
    if version_id is not None:
        row = get_version(int(version_id))
    if row is None:
        row = active_version_row() or get_version_by_number(BASELINE_VERSION)
    if row is None:  # extremely cold path: unseeded database
        ensure_baseline_version()
        row = active_version_row()
    return RuleSpec(dict(row["spec"]))


def active_version_row() -> dict[str, Any] | None:
    return pool.query_one("SELECT * FROM merge_rule_versions WHERE status = 'active' LIMIT 1")


def active_version_id() -> int:
    row = active_version_row()
    if row:
        return int(row["id"])
    return ensure_baseline_version()


def baseline_id() -> int:
    row = get_version_by_number(BASELINE_VERSION)
    return int(row["id"]) if row else ensure_baseline_version()


def create_version(spec: dict[str, Any], *, name: str, changelog: str | None = None,
                   created_by: str = "operator") -> dict[str, Any]:
    validate_spec(spec)
    with pool.tx() as conn:
        cur = conn.execute("SELECT coalesce(max(version), 0) + 1 AS next FROM merge_rule_versions")
        next_version = int(cur.fetchone()["next"])
        cur.close()
        cur = conn.execute(
            """INSERT INTO merge_rule_versions (version, name, status, spec, changelog, created_by)
               VALUES (%s, %s, 'draft', %s::jsonb, %s, %s) RETURNING *""",
            (next_version, name, json.dumps(spec, ensure_ascii=False), changelog, created_by))
        row = cur.fetchone()
        cur.close()
        return dict(row)


# ---------------------------------------------------------------------------
# rollout (percentage gray release per platform)
# ---------------------------------------------------------------------------
def set_rollout(version_id: int, rollouts: dict[str, int], *, actor: str = "operator") -> dict[str, Any]:
    row = get_version(version_id)
    if not row:
        raise ValueError(f"规则版本 {version_id} 不存在")
    if row["status"] not in ("active",):
        raise ValueError("只有 active 版本可以调整放量比例（请先激活）")
    clean: dict[str, int] = {}
    for plat, pct in rollouts.items():
        p = plat.strip().upper()
        if p != "*" and p not in ("A", "B", "C", "D"):
            raise ValueError(f"未知平台 {plat}（可选 A/B/C/D 或 *）")
        if not isinstance(pct, int) or not 0 <= pct <= 100:
            raise ValueError("放量比例必须是 0~100 的整数")
        clean[p] = pct
    with pool.tx() as conn:
        for plat, pct in clean.items():
            conn.execute(
                """INSERT INTO merge_rule_rollouts (rule_version_id, platform, rollout_pct, updated_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (rule_version_id, platform) DO UPDATE
                     SET rollout_pct = EXCLUDED.rollout_pct,
                         updated_by = EXCLUDED.updated_by, updated_at = now()""",
                (version_id, plat, pct, actor))
    return {"rule_version_id": version_id, "rollouts": get_rollout(version_id)}


def get_rollout(version_id: int) -> dict[str, int]:
    rows = pool.query(
        "SELECT platform, rollout_pct FROM merge_rule_rollouts WHERE rule_version_id = %s",
        (version_id,))
    return {r["platform"]: int(r["rollout_pct"]) for r in rows}


def _bucket(platform: str, listing_id: int) -> int:
    """Stable 0..99 bucket: same listing always lands in the same shard."""
    digest = hashlib.md5(f"{platform}:{listing_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def rollout_pct_for(version_id: int, platform: str) -> int:
    mapping = get_rollout(version_id)
    if not mapping:
        # The baseline (or any version without explicit rows) is fully on.
        return 100
    return int(mapping.get(platform.upper(), mapping.get("*", 0)))


def select_version_id(platform: str, listing_id: int) -> int:
    """Decide which rule version judges this listing.

    The active version only sees traffic inside its platform rollout pct;
    everything else keeps using the v1 baseline. Hash bucketing is stable,
    so widening the rollout never flips a listing back and forth.
    """
    active = active_version_row()
    base = baseline_id()
    if not active or int(active["id"]) == base:
        return base
    active_id = int(active["id"])
    pct = rollout_pct_for(active_id, platform)
    if pct <= 0:
        return base
    if pct >= 100 or _bucket(platform, listing_id) < pct:
        return active_id
    return base


# ---------------------------------------------------------------------------
# activation / rollback
# ---------------------------------------------------------------------------
def latest_replay(version_id: int) -> dict[str, Any] | None:
    return pool.query_one(
        "SELECT * FROM merge_rule_replays WHERE rule_version_id = %s ORDER BY id DESC LIMIT 1",
        (version_id,))


def _approval_thresholds() -> dict[str, int]:
    return {
        "max_extra_merges": int(settings.replay_max_extra_merges),
        "max_split_confirmed": int(settings.replay_max_split_confirmed),
    }


def activate_version(version_id: int, *, actor: str = "operator",
                     initial_rollout: dict[str, int] | None = None) -> dict[str, Any]:
    row = get_version(version_id)
    if not row:
        raise ValueError(f"规则版本 {version_id} 不存在")
    if row["status"] == "active":
        raise ValueError("该版本已经是 active")
    replay = latest_replay(version_id)
    if replay is None:
        raise ValueError("上线前必须先在历史存量上离线回放（carscout rules replay）")
    thresholds = _approval_thresholds()
    if replay["exceeds_threshold"] and replay["status"] != "approved":
        raise ValueError(
            f"回放影响超过审批门槛 {thresholds}（新增合并 {replay['extra_merges']} / "
            f"拆散人工确认 {replay['split_confirmed']}），必须先人工审批该回放")
    with pool.tx() as conn:
        conn.execute("UPDATE merge_rule_versions SET status = 'inactive' WHERE status = 'active'")
        conn.execute(
            """UPDATE merge_rule_versions
               SET status = 'active', activated_at = now(), approved_by = %s, updated_at = now()
               WHERE id = %s""",
            (actor, version_id))
        # safe default: zero traffic until an explicit rollout widens it
        rollout_items = list(initial_rollout.items()) if initial_rollout else [("*", 0)]
        for plat, pct in rollout_items:
            conn.execute(
                """INSERT INTO merge_rule_rollouts (rule_version_id, platform, rollout_pct, updated_by)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (rule_version_id, platform) DO UPDATE
                     SET rollout_pct = EXCLUDED.rollout_pct, updated_at = now()""",
                (version_id, plat.strip().upper(), int(pct), actor))
    return {"activated": version_id, "rollouts": get_rollout(version_id)}


def rollback_active(*, actor: str = "operator", reason: str = "") -> dict[str, Any]:
    """Revert to the previous version.

    Records merged under the rolled-back version are flagged
    `pending_rereview` but KEEP their vehicle_id: we never auto-split, so
    historical vehicle/price statistics do not jump. A human re-confirms or
    re-clusters them through the review queue.
    """
    active = active_version_row()
    if not active:
        raise ValueError("当前没有 active 规则版本")
    active_id = int(active["id"])
    if active_id == baseline_id():
        raise ValueError("基线版本 v1 无可回滚的上一版本")
    previous = pool.query_one(
        """SELECT * FROM merge_rule_versions WHERE status = 'inactive'
           ORDER BY version DESC LIMIT 1""")
    with pool.tx() as conn:
        conn.execute(
            """UPDATE merge_rule_versions
               SET status = 'rollback', rolled_back_at = now(),
                   changelog = COALESCE(changelog, '') || %s, updated_at = now()
               WHERE id = %s""",
            (f"\n[rollback] {reason}".rstrip(), active_id))
        if previous:
            conn.execute(
                "UPDATE merge_rule_versions SET status = 'active', activated_at = now() WHERE id = %s",
                (previous["id"],))
        # flag every listing merged under the outgoing rule; do NOT detach.
        cur = conn.execute(
            """UPDATE listings
               SET match_status = 'pending_rereview',
                   pending_rereview_reason = %s, updated_at = now()
               WHERE rule_version_id = %s
                 AND match_status IN ('auto', 'ruled')
               RETURNING id""",
            (f"rule v{active['version']} rolled back: {reason or 'manual rollback'}", active_id))
        affected = [r["id"] for r in cur.fetchall()]
        cur.close()
        # surface the affected pairs in the existing human review queue
        batch = f"rollback-v{active['version']}"
        cur = conn.execute(
            """SELECT DISTINCT c.id AS candidate_id FROM match_candidates c
               WHERE c.rule_version_id = %s
                 AND c.state IN ('auto_merged', 'rule_applied')""", (active_id,))
        candidate_ids = [r["candidate_id"] for r in cur.fetchall()]
        cur.close()
        for cid in candidate_ids:
            conn.execute(
                """INSERT INTO manual_review_queue (candidate_id, batch_id, status, note)
                   VALUES (%s, %s, 'pending', %s)
                   ON CONFLICT (candidate_id, batch_id) DO NOTHING""",
                (cid, batch, f"规则回滚待复核：{reason or 'manual rollback'}"))
    return {
        "rolled_back_version": active_id,
        "reactivated_version": int(previous["id"]) if previous else None,
        "listings_pending_rereview": len(affected),
        "review_batch": batch,
        "review_candidates": len(candidate_ids),
    }


def pending_rereview_listings(limit: int = 500) -> list[dict[str, Any]]:
    return pool.query(
        """SELECT l.id, l.platform, l.platform_listing_id, l.vehicle_id, l.rule_version_id,
                  l.pending_rereview_reason, l.updated_at
           FROM listings l WHERE l.match_status = 'pending_rereview'
           ORDER BY l.updated_at DESC LIMIT %s""", (limit,))


# ---------------------------------------------------------------------------
# offline replay
# ---------------------------------------------------------------------------
def replay_version(version_id: int, *, actor: str = "operator") -> dict[str, Any]:
    """Re-cluster the whole active corpus in memory under the candidate spec
    and under the current baseline, then diff the two outcomes.

    Nothing in the database is re-clustered: the replay is read-only over
    listings/rules and only writes a merge_rule_replays audit row.
    """
    from carscout.matching import simulate_clustering

    row = get_version(version_id)
    if not row:
        raise ValueError(f"规则版本 {version_id} 不存在")
    target_spec = RuleSpec(dict(row["spec"]))
    base_row = active_version_row()
    if base_row and base_row["id"] != version_id:
        base_spec = RuleSpec(dict(base_row["spec"]))
        base_version_id = int(base_row["id"])
    else:
        base_row = get_version_by_number(BASELINE_VERSION)
        base_spec = RuleSpec(dict(base_row["spec"]))
        base_version_id = int(base_row["id"])

    target_sim = simulate_clustering(target_spec)
    baseline_sim = simulate_clustering(base_spec)

    # 1) pairs the candidate rule newly merges (together in target, apart in baseline)
    extra_merges = 0
    changed_samples: list[dict[str, Any]] = []
    for cluster in target_sim["clusters"]:
        members = sorted(cluster["members"])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if baseline_sim["partition"].get(a) != baseline_sim["partition"].get(b):
                    extra_merges += 1
                    if len(changed_samples) < 50:
                        changed_samples.append(
                            _replay_sample(a, b, baseline_sim, target_sim, "new_merge"))

    # 2) human-confirmed pairs the candidate rule would split apart
    confirmed = pool.query(
        """SELECT listing_a, listing_b FROM match_candidates WHERE state = 'manual_confirmed'""")
    split_confirmed = 0
    for c in confirmed:
        a, b = c["listing_a"], c["listing_b"]
        if target_sim["partition"].get(a) is not None and \
                target_sim["partition"].get(a) != target_sim["partition"].get(b):
            split_confirmed += 1
            if len(changed_samples) < 50:
                changed_samples.append(
                    _replay_sample(a, b, baseline_sim, target_sim, "split_manual_confirmed"))

    # 3) decision drift on directly evaluated pairs
    extra_gray = extra_rejects = unchanged = 0
    pair_keys = set(baseline_sim["pair_decisions"]) | set(target_sim["pair_decisions"])
    for key in pair_keys:
        b_dec = baseline_sim["pair_decisions"].get(key)
        t_dec = target_sim["pair_decisions"].get(key)
        if b_dec and t_dec and b_dec["decision"] == t_dec["decision"]:
            unchanged += 1
        elif t_dec and t_dec["decision"] == "gray":
            extra_gray += 1
        elif t_dec and t_dec["decision"] == "reject":
            extra_rejects += 1

    thresholds = _approval_thresholds()
    exceeds = (extra_merges > thresholds["max_extra_merges"]
               or split_confirmed > thresholds["max_split_confirmed"])
    pairs_evaluated = len(target_sim["pair_decisions"])
    report = {
        "samples": changed_samples,
        "target_clusters": len(target_sim["clusters"]),
        "baseline_clusters": len(baseline_sim["clusters"]),
        "listings_evaluated": target_sim["listings_evaluated"],
    }
    with pool.tx() as conn:
        cur = conn.execute(
            """INSERT INTO merge_rule_replays
               (rule_version_id, baseline_version_id, status, pairs_evaluated, extra_merges,
                split_confirmed, extra_gray, extra_rejects, unchanged, approval_threshold,
                exceeds_threshold, report, requested_by)
               VALUES (%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)
               RETURNING *""",
            (version_id, base_version_id,
             pairs_evaluated, extra_merges, split_confirmed, extra_gray, extra_rejects,
             unchanged, json.dumps(thresholds), exceeds,
             json.dumps(report, ensure_ascii=False, default=str), actor))
        inserted = dict(cur.fetchone())
        cur.close()
    return inserted


def _replay_sample(a: int, b: int, baseline_sim: dict, target_sim: dict, kind: str) -> dict[str, Any]:
    lo, hi = sorted((a, b))
    b_dec = baseline_sim["pair_decisions"].get((lo, hi)) or {}
    t_dec = target_sim["pair_decisions"].get((lo, hi)) or {}
    return {
        "kind": kind, "listing_a": lo, "listing_b": hi,
        "baseline_decision": b_dec.get("decision"), "baseline_score": b_dec.get("score"),
        "target_decision": t_dec.get("decision"), "target_score": t_dec.get("score"),
        "target_evidence": t_dec.get("evidence"),
    }


def decide_replay(replay_id: int, approve: bool, *, reviewer: str = "operator",
                  note: str | None = None) -> dict[str, Any]:
    row = pool.query_one("SELECT * FROM merge_rule_replays WHERE id = %s", (replay_id,))
    if not row:
        raise ValueError(f"回放记录 {replay_id} 不存在")
    status = "approved" if approve else "rejected"
    pool.execute(
        "UPDATE merge_rule_replays SET status = %s, reviewed_by = %s, reviewed_at = now() WHERE id = %s",
        (status, reviewer, replay_id))
    if note:
        report = dict(row["report"] or {})
        report["review_note"] = note
        pool.execute("UPDATE merge_rule_replays SET report = %s::jsonb WHERE id = %s",
                     (json.dumps(report, ensure_ascii=False), replay_id))
    return {"replay_id": replay_id, "status": status}


def list_replays(version_id: int | None = None) -> list[dict[str, Any]]:
    return pool.query(
        """SELECT * FROM merge_rule_replays
           WHERE (%s::int IS NULL OR rule_version_id = %s)
           ORDER BY id DESC""", (version_id, version_id))
