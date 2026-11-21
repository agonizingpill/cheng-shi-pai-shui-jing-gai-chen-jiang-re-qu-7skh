"""聚类参数对照试算。

同批测点分别用现行默认参数与候选参数各跑一次 DBSCAN，产出对照报告。
试算只读测点、只写 ParameterTrial，绝不改动 RepairZone / SurveyPoint 等生产聚类结果。
"""
import hashlib
import numpy as np
from sklearn.cluster import DBSCAN
from sqlalchemy import select

from ..models import ParameterTrial, SurveyPoint

EPS_LIMITS = (1.0, 200.0)
MIN_SAMPLES_LIMITS = (2, 20)


def snapshot_points(db, batch):
    """取批次当前全部测点的快照（按 id 稳定排序）。"""
    rows = db.execute(
        select(SurveyPoint).where(SurveyPoint.batch_id == batch.id).order_by(SurveyPoint.id)
    ).scalars().all()
    return [
        {
            "id": p.id,
            "point_code": p.point_code,
            "manhole_id": p.manhole_id,
            "x": p.x,
            "y": p.y,
            "settlement_mm": p.settlement_mm,
            "coordinate_reference": p.coordinate_reference,
            "quality_status": p.quality_status,
        }
        for p in rows
    ]


def snapshot_hash(points):
    """测点快照指纹：测点集合或坐标/基准/沉降任一字段变化都会改变。"""
    digest = hashlib.sha256()
    for p in points:
        digest.update(
            repr((p["id"], p["point_code"], p["x"], p["y"], p["settlement_mm"],
                  p["coordinate_reference"], p["quality_status"])).encode("utf-8")
        )
        digest.update(b"|")
    return digest.hexdigest()


def run_dbscan(points, eps_m, min_samples):
    if not points:
        return np.array([], dtype=int)
    matrix = np.array([[p["x"], p["y"]] for p in points], dtype=float)
    return DBSCAN(eps=float(eps_m), min_samples=int(min_samples)).fit_predict(matrix)


def label_map(points, labels):
    return {p["id"]: int(label) for p, label in zip(points, labels)}


def cluster_summaries(points, labels):
    result = []
    for label in sorted({int(x) for x in labels}):
        if label == -1:
            continue
        members = [p for p, current in zip(points, labels) if int(current) == label]
        xs = [p["x"] for p in members]
        ys = [p["y"] for p in members]
        settlements = [p["settlement_mm"] for p in members]
        result.append({
            "label": label,
            "point_count": len(members),
            "point_codes": [p["point_code"] for p in members],
            "center_x": round(sum(xs) / len(xs), 2),
            "center_y": round(sum(ys) / len(ys), 2),
            "average_settlement_mm": round(sum(settlements) / len(settlements), 2),
            "max_settlement_mm": round(max(settlements), 2),
        })
    return result


def compare(points, baseline_params, candidate_params):
    """两次试算并生成对照报告。"""
    baseline_labels = run_dbscan(points, **baseline_params)
    candidate_labels = run_dbscan(points, **candidate_params)
    base_map, cand_map = label_map(points, baseline_labels), label_map(points, candidate_labels)

    hot_to_scattered, scattered_to_hot, changed_membership = [], [], []
    for p in points:
        before, after = base_map[p["id"]], cand_map[p["id"]]
        was_hot, is_hot = before != -1, after != -1
        entry = {
            "point_id": p["id"],
            "point_code": p["point_code"],
            "manhole_id": p["manhole_id"],
            "baseline_label": None if before == -1 else before,
            "candidate_label": None if after == -1 else after,
            "settlement_mm": p["settlement_mm"],
        }
        if was_hot and not is_hot:
            hot_to_scattered.append(entry)
        elif not was_hot and is_hot:
            scattered_to_hot.append(entry)
        elif before != after:
            changed_membership.append(entry)

    baseline_summary = {
        "params": {"eps_m": baseline_params["eps_m"], "min_samples": baseline_params["min_samples"]},
        "cluster_count": len({int(x) for x in baseline_labels} - {-1}),
        "noise_count": int(sum(1 for x in baseline_labels if int(x) == -1)),
        "clusters": cluster_summaries(points, baseline_labels),
    }
    candidate_summary = {
        "params": {"eps_m": candidate_params["eps_m"], "min_samples": candidate_params["min_samples"]},
        "cluster_count": len({int(x) for x in candidate_labels} - {-1}),
        "noise_count": int(sum(1 for x in candidate_labels if int(x) == -1)),
        "clusters": cluster_summaries(points, candidate_labels),
    }
    return {
        "point_count": len(points),
        "baseline": baseline_summary,
        "candidate": candidate_summary,
        "diff": {
            "hot_to_scattered_count": len(hot_to_scattered),
            "scattered_to_hot_count": len(scattered_to_hot),
            "changed_membership_count": len(changed_membership),
            "hot_to_scattered": hot_to_scattered,
            "scattered_to_hot": scattered_to_hot,
            "changed_membership": changed_membership,
        },
    }


def build_trial(db, batch, baseline_params, candidate_params):
    points = snapshot_points(db, batch)
    if not points:
        raise ValueError("批次还没有测量点，无法试算")
    report = compare(points, baseline_params, candidate_params)
    return points, snapshot_hash(points), report


def mark_open_trials_stale(db, batch_id, reason):
    """测点发生变动后，该批次所有未签批的对照报告立即过期。"""
    rows = db.execute(
        select(ParameterTrial).where(
            ParameterTrial.batch_id == batch_id, ParameterTrial.status == "open"
        )
    ).scalars().all()
    for trial in rows:
        trial.status = "stale"
        trial.stale_reason = reason
    return len(rows)
