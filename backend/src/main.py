import os, json, logging
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from redis import Redis

from .database import Base, SessionLocal, engine, get_db
from .models import AnalysisJob, AuditLog, ClusterSetting, ManholeAsset, ParameterTrial, RepairZone, Resurvey, SurveyBatch, SurveyPoint, User, WorkNotice
from .schemas import BatchIn, LoginIn, NoticeIn, ParameterTrialIn, ResurveyIn, SurveyPointIn
from .security import create_token, current_user, require_roles, verify_password
from .services.audit import record
from .services.cluster import DEFAULT_CLUSTER_PARAMS, build_input
from .services.geometry import bounds, overlaps
from .services.trial import EPS_LIMITS, MIN_SAMPLES_LIMITS, build_trial, mark_open_trials_stale, snapshot_hash, snapshot_points
from .middlewares.audit import RequestAuditMiddleware
from .middlewares.rate_limit import SimpleRateLimitMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manhole-settlement")
app = FastAPI(title="城市排水井盖沉降热区", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(RequestAuditMiddleware)
app.add_middleware(SimpleRateLimitMiddleware)
redis_client = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)

@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if not db.execute(select(ClusterSetting)).scalar_one_or_none():
            db.add(ClusterSetting(**DEFAULT_CLUSTER_PARAMS))
            db.commit()
    finally:
        db.close()
    if os.getenv("SEED_DEMO", "true").lower() == "true":
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:
            logger.exception("seed_demo failed during startup")

@app.get("/health")
def health():
    return {"status": "ok", "service": "manhole-settlement-api"}

def iso(value):
    return value.isoformat() if value else None

def user_view(user):
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}

def asset_view(a):
    return {"id": a.id, "asset_code": a.asset_code, "street_name": a.street_name, "x": a.x, "y": a.y, "coordinate_reference": a.coordinate_reference, "cover_type": a.cover_type, "depth_mm": a.depth_mm, "status": a.status, "last_inspected_at": iso(a.last_inspected_at)}

def point_view(p):
    return {"id": p.id, "point_code": p.point_code, "batch_id": p.batch_id, "manhole_id": p.manhole_id, "asset_code": p.manhole.asset_code if p.manhole else "", "street_name": p.manhole.street_name if p.manhole else "", "x": p.x, "y": p.y, "elevation_mm": p.elevation_mm, "settlement_mm": p.settlement_mm, "coordinate_reference": p.coordinate_reference, "measured_at": iso(p.measured_at), "quality_status": p.quality_status, "cluster_label": p.cluster_label, "local_density": p.local_density}

def zone_view(z, db):
    notice = db.get(WorkNotice, z.conflict_notice_id) if z.conflict_notice_id else None
    point_rows = db.execute(select(SurveyPoint).where(SurveyPoint.id.in_(z.point_ids or []))).scalars().all() if z.point_ids else []
    return {"id": z.id, "zone_code": z.zone_code, "batch_id": z.batch_id, "cluster_label": z.cluster_label, "name": z.name, "status": z.status, "point_ids": z.point_ids or [], "point_codes": [p.point_code for p in point_rows], "center_x": z.center_x, "center_y": z.center_y, "average_settlement_mm": z.average_settlement_mm, "max_settlement_mm": z.max_settlement_mm, "local_density": z.local_density, "boundary": z.boundary, "original_cluster_evidence": z.original_cluster_evidence, "split_from_id": z.split_from_id, "conflict_notice_id": z.conflict_notice_id, "split_reason": z.split_reason, "conflict_road": notice.road_name if notice else None}

def notice_view(n):
    return {"id": n.id, "notice_code": n.notice_code, "zone_id": n.zone_id, "road_name": n.road_name, "contractor": n.contractor, "permit_no": n.permit_no, "starts_at": iso(n.starts_at), "ends_at": iso(n.ends_at), "boundary": n.boundary, "status": n.status, "lane_closure_required": n.lane_closure_required, "conflict_note": n.conflict_note}

def resurvey_view(r):
    return {"id": r.id, "record_code": r.record_code, "zone_id": r.zone_id, "measured_at": iso(r.measured_at), "settlement_before_mm": r.settlement_before_mm, "settlement_after_mm": r.settlement_after_mm, "delta_mm": r.delta_mm, "pass_fail": r.pass_fail, "status": r.status, "verified_by": r.verified_by, "notes": r.notes}

def get_cluster_setting(db):
    setting = db.execute(select(ClusterSetting).where(ClusterSetting.id == 1)).scalar_one_or_none()
    if not setting:
        setting = ClusterSetting(id=1, **DEFAULT_CLUSTER_PARAMS)
        db.add(setting); db.commit(); db.refresh(setting)
    return setting

def setting_view(setting):
    return {"eps_m": setting.eps_m, "min_samples": setting.min_samples, "updated_at": iso(setting.updated_at), "updated_by": setting.updated_by}

def trial_view(t):
    return {"id": t.id, "trial_code": t.trial_code, "batch_id": t.batch_id, "status": t.status,
            "baseline_eps_m": t.baseline_eps_m, "baseline_min_samples": t.baseline_min_samples,
            "candidate_eps_m": t.candidate_eps_m, "candidate_min_samples": t.candidate_min_samples,
            "point_snapshot_hash": t.point_snapshot_hash, "report": t.report, "stale_reason": t.stale_reason,
            "approved_by": t.approved_by, "approved_at": iso(t.approved_at), "created_at": iso(t.created_at),
            "created_by": t.created_by}

@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {"access_token": create_token(user), "token_type": "bearer", "user": user_view(user)}

@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_view(user)

@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    batches = db.execute(select(SurveyBatch).order_by(SurveyBatch.measured_at.desc())).scalars().all()
    assets = db.execute(select(ManholeAsset).order_by(ManholeAsset.asset_code)).scalars().all()
    points = db.execute(select(SurveyPoint).order_by(SurveyPoint.id)).scalars().all()
    zones = db.execute(select(RepairZone).order_by(RepairZone.id)).scalars().all()
    notices = db.execute(select(WorkNotice).order_by(WorkNotice.id)).scalars().all()
    resurveys = db.execute(select(Resurvey).order_by(Resurvey.id.desc())).scalars().all()
    jobs = db.execute(select(AnalysisJob).order_by(AnalysisJob.id.desc())).scalars().all()
    setting = get_cluster_setting(db)
    trials = db.execute(select(ParameterTrial).order_by(ParameterTrial.id.desc())).scalars().all()
    return {"user": user_view(user), "cluster_setting": setting_view(setting), "parameter_trials": [trial_view(t) for t in trials], "stats": {"assets": len(assets), "points": len(points), "zones": len(zones), "open_zones": sum(z.status not in ["closed", "split"] for z in zones), "notices": len(notices), "resurveys": len(resurveys)}, "batches": [{"id": b.id, "batch_code": b.batch_code, "measured_at": iso(b.measured_at), "instrument": b.instrument, "coordinate_reference": b.coordinate_reference, "status": b.status, "notes": b.notes, "point_count": len(b.points)} for b in batches], "assets": [asset_view(a) for a in assets], "points": [point_view(p) for p in points], "zones": [zone_view(z, db) for z in zones], "notices": [notice_view(n) for n in notices], "resurveys": [resurvey_view(r) for r in resurveys], "jobs": [{"id": j.id, "job_code": j.job_code, "batch_id": j.batch_id, "status": j.status, "result": j.result, "error": j.error, "finished_at": iso(j.finished_at)} for j in jobs]}

@app.get("/api/survey-batches/{batch_id}")
def batch_detail(batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    batch = db.get(SurveyBatch, batch_id)
    if not batch:
        raise HTTPException(404, "测量批次不存在")
    return {"id": batch.id, "batch_code": batch.batch_code, "measured_at": iso(batch.measured_at), "instrument": batch.instrument, "coordinate_reference": batch.coordinate_reference, "status": batch.status, "notes": batch.notes, "points": [point_view(p) for p in batch.points]}

@app.post("/api/survey-batches")
def create_batch(payload: BatchIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    if db.execute(select(SurveyBatch).where(SurveyBatch.batch_code == payload.batch_code)).scalar_one_or_none():
        raise HTTPException(409, "批次编号已存在")
    batch = SurveyBatch(batch_code=payload.batch_code, measured_at=payload.measured_at, instrument=payload.instrument, coordinate_reference=payload.coordinate_reference, notes=payload.notes, status="open")
    db.add(batch); record(db, user.id, "create_batch", "SurveyBatch", payload.batch_code, {"coordinate_reference": payload.coordinate_reference}); db.commit(); db.refresh(batch)
    return {"id": batch.id, "batch_code": batch.batch_code, "status": batch.status}

@app.post("/api/survey-batches/{batch_id}/points")
def add_point(batch_id: int, payload: SurveyPointIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = db.get(SurveyBatch, batch_id)
    asset = db.get(ManholeAsset, payload.manhole_id)
    if not batch or not asset:
        raise HTTPException(404, "批次或井盖资产不存在")
    if batch.status in ["clustered", "queued"]:
        raise HTTPException(409, "批次已进入聚类流程，不能追加测量点")
    if db.execute(select(SurveyPoint).where(SurveyPoint.point_code == payload.point_code)).scalar_one_or_none():
        raise HTTPException(409, "测量点编号已存在")
    point = SurveyPoint(batch_id=batch_id, **payload.model_dump())
    db.add(point); db.flush()
    stale_count = mark_open_trials_stale(db, batch_id, f"测量点 {payload.point_code} 新增，测点集合已变化，对照报告过期需重算")
    record(db, user.id, "add_survey_point", "SurveyPoint", payload.point_code, {"batch_code": batch.batch_code, "stale_trials": stale_count}); db.commit(); db.refresh(point)
    return point_view(point)

@app.post("/api/survey-points/{point_id}/correct-datum")
def correct_datum(point_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    point = db.get(SurveyPoint, point_id)
    if not point:
        raise HTTPException(404, "测量点不存在")
    batch = db.get(SurveyBatch, point.batch_id)
    old = point.coordinate_reference; point.coordinate_reference = batch.coordinate_reference; point.quality_status = "datum_corrected"
    if batch.status == "blocked": batch.status = "ready"
    stale_count = mark_open_trials_stale(db, point.batch_id, f"测量点 {point.point_code} 坐标基准由 {old} 修正为 {point.coordinate_reference}，对照报告过期需重算")
    record(db, user.id, "correct_coordinate_reference", "SurveyPoint", point.point_code, {"from": old, "to": point.coordinate_reference, "stale_trials": stale_count}); db.commit()
    return {"point_code": point.point_code, "coordinate_reference": point.coordinate_reference, "batch_status": batch.status}

@app.post("/api/survey-batches/{batch_id}/cluster")
def enqueue_cluster(batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    batch = db.get(SurveyBatch, batch_id)
    if not batch:
        raise HTTPException(404, "测量批次不存在")
    points = build_input(db, batch)
    refs = {batch.coordinate_reference, *(p["coordinate_reference"] for p in points)}
    if len(refs) != 1:
        batch.status = "blocked"
        record(db, user.id, "block_cluster_datum_conflict", "SurveyBatch", batch.batch_code, {"references": sorted(refs)})
        db.commit()
        conflicts = [p["point_code"] for p in points if p["coordinate_reference"] != batch.coordinate_reference]
        raise HTTPException(status_code=409, detail={"code": "DATUM_CONFLICT", "message": "测量点存在坐标基准冲突，禁止聚类", "point_codes": conflicts, "batch_status": batch.status})
    setting = get_cluster_setting(db)
    params = {"eps_m": setting.eps_m, "min_samples": setting.min_samples}
    job = AnalysisJob(job_code=f"JOB-{batch.batch_code}-{int(datetime.now().timestamp())}", batch_id=batch.id, status="queued", input_summary={"points": points, "params": params})
    batch.status = "queued"; db.add(job); record(db, user.id, "enqueue_spatial_cluster", "AnalysisJob", job.job_code, {"batch_code": batch.batch_code, "point_count": len(points), "params": params}); db.commit(); db.refresh(job)
    redis_client.rpush("manhole_settlement_jobs", str(job.id))
    return {"job_id": job.id, "job_code": job.job_code, "status": job.status}

@app.get("/api/analysis-jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = db.get(AnalysisJob, job_id)
    if not job: raise HTTPException(404, "分析任务不存在")
    return {"id": job.id, "job_code": job.job_code, "status": job.status, "result": job.result, "error": job.error, "finished_at": iso(job.finished_at)}

@app.get("/api/cluster-settings")
def cluster_settings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return setting_view(get_cluster_setting(db))

@app.get("/api/parameter-trials")
def list_trials(batch_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(ParameterTrial).order_by(ParameterTrial.id.desc())
    if batch_id is not None:
        stmt = stmt.where(ParameterTrial.batch_id == batch_id)
    return [trial_view(t) for t in db.execute(stmt).scalars().all()]

@app.get("/api/parameter-trials/{trial_id}")
def trial_detail(trial_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    trial = db.get(ParameterTrial, trial_id)
    if not trial: raise HTTPException(404, "对照试算报告不存在")
    return trial_view(trial)

def _prepare_trial(db, batch, payload):
    setting = get_cluster_setting(db)
    baseline = {"eps_m": setting.eps_m, "min_samples": setting.min_samples}
    candidate = {"eps_m": round(float(payload.eps_m), 2), "min_samples": int(payload.min_samples)}
    if not (EPS_LIMITS[0] <= candidate["eps_m"] <= EPS_LIMITS[1]):
        raise HTTPException(422, f"eps_m 必须在 {EPS_LIMITS[0]}–{EPS_LIMITS[1]} 米之间")
    if not (MIN_SAMPLES_LIMITS[0] <= candidate["min_samples"] <= MIN_SAMPLES_LIMITS[1]):
        raise HTTPException(422, f"min_samples 必须在 {MIN_SAMPLES_LIMITS[0]}–{MIN_SAMPLES_LIMITS[1]} 之间")
    if candidate == baseline:
        raise HTTPException(409, "候选参数与现行默认参数相同，无需对照试算")
    points = build_input(db, batch)
    refs = {batch.coordinate_reference, *(p["coordinate_reference"] for p in points)}
    if len(refs) != 1:
        raise HTTPException(status_code=409, detail={"code": "DATUM_CONFLICT", "message": "测量点存在坐标基准冲突，先统一基准再试算"})
    try:
        snap, digest, report = build_trial(db, batch, baseline, candidate)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return setting, baseline, candidate, snap, digest, report

def _persist_trial(db, *, batch, user, baseline, candidate, snap, digest, report, source_trial=None):
    import secrets
    trial = ParameterTrial(
        trial_code=f"TRIAL-{batch.batch_code.replace('SURV-2026-', '')}-{int(datetime.now().timestamp())}-{secrets.token_hex(2)}",
        batch_id=batch.id, created_by=user.id, status="open",
        baseline_eps_m=baseline["eps_m"], baseline_min_samples=baseline["min_samples"],
        candidate_eps_m=candidate["eps_m"], candidate_min_samples=candidate["min_samples"],
        point_snapshot_hash=digest, point_snapshot=snap, report=report,
    )
    db.add(trial); db.flush()
    record(db, user.id, "create_parameter_trial", "ParameterTrial", trial.trial_code,
           {"batch_code": batch.batch_code, "baseline": baseline, "candidate": candidate,
            "hot_to_scattered": report["diff"]["hot_to_scattered_count"],
            "source_trial": source_trial.trial_code if source_trial else None})
    db.commit(); db.refresh(trial)
    return trial

@app.post("/api/survey-batches/{batch_id}/parameter-trials")
def create_trial(batch_id: int, payload: ParameterTrialIn, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    batch = db.get(SurveyBatch, batch_id)
    if not batch: raise HTTPException(404, "测量批次不存在")
    _, baseline, candidate, snap, digest, report = _prepare_trial(db, batch, payload)
    trial = _persist_trial(db, batch=batch, user=user, baseline=baseline, candidate=candidate, snap=snap, digest=digest, report=report)
    return trial_view(trial)

@app.post("/api/parameter-trials/{trial_id}/retrial")
def retrial(trial_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    """报告过期或默认值已变后，用最新测点与最新默认参数重新对照试算。"""
    source = db.get(ParameterTrial, trial_id)
    if not source: raise HTTPException(404, "对照试算报告不存在")
    if source.status == "approved":
        raise HTTPException(409, "已签批生效的报告不能重算")
    batch = db.get(SurveyBatch, source.batch_id)
    payload = ParameterTrialIn(eps_m=source.candidate_eps_m, min_samples=source.candidate_min_samples)
    _, baseline, candidate, snap, digest, report = _prepare_trial(db, batch, payload)
    trial = _persist_trial(db, batch=batch, user=user, baseline=baseline, candidate=candidate, snap=snap, digest=digest, report=report, source_trial=source)
    return trial_view(trial)

@app.post("/api/parameter-trials/{trial_id}/approve")
def approve_trial(trial_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    # 锁默认参数单行，串行化并发签批（SQLite 不支持行锁，测试环境退化为普通读）
    setting_stmt = select(ClusterSetting).where(ClusterSetting.id == 1)
    if db.get_bind().dialect.name == "postgresql":
        setting_stmt = setting_stmt.with_for_update()
    setting = db.execute(setting_stmt).scalar_one_or_none()
    if not setting:
        setting = get_cluster_setting(db)
    trial = db.get(ParameterTrial, trial_id)
    if not trial: raise HTTPException(404, "对照试算报告不存在")
    if trial.status == "approved":
        raise HTTPException(409, "该报告已经签批生效")
    if trial.status == "stale":
        raise HTTPException(status_code=409, detail={"code": "TRIAL_STALE", "message": "测点已变动，报告已过期，请重算后再签批", "stale_reason": trial.stale_reason})
    # 权威过期校验：即使并发的测点变动尚未触发标记，快照指纹不一致也拒绝签批
    current_digest = snapshot_hash(snapshot_points(db, db.get(SurveyBatch, trial.batch_id)))
    if current_digest != trial.point_snapshot_hash:
        trial.status = "stale"
        trial.stale_reason = "签批时发现测点快照与试算时不一致，自动置为过期"
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "TRIAL_STALE", "message": "测点已变动，报告已过期，请重算后再签批", "stale_reason": trial.stale_reason})
    # 默认值已被另一份先签批的报告更新：本报告必须与新默认值重新比较
    current_default = {"eps_m": setting.eps_m, "min_samples": setting.min_samples}
    trial_baseline = {"eps_m": trial.baseline_eps_m, "min_samples": trial.baseline_min_samples}
    if current_default != trial_baseline:
        trial.status = "stale"
        trial.stale_reason = (f"默认参数已变为 eps={current_default['eps_m']}m / min_samples={current_default['min_samples']}，"
                              "本报告的对照基线失效，需与新默认值重新比较")
        db.commit()
        raise HTTPException(status_code=409, detail={"code": "TRIAL_BASELINE_CHANGED", "message": trial.stale_reason, "current_default": current_default})
    setting.eps_m = trial.candidate_eps_m
    setting.min_samples = trial.candidate_min_samples
    setting.updated_by = user.id
    setting.updated_at = datetime.now(timezone.utc)
    trial.status = "approved"
    trial.approved_by = user.id
    trial.approved_at = datetime.now(timezone.utc)
    # 并发签批：其余未签批报告全部过期，必须与新默认值重新比较
    others = db.execute(select(ParameterTrial).where(ParameterTrial.id != trial.id, ParameterTrial.status == "open")).scalars().all()
    for other in others:
        other.status = "stale"
        other.stale_reason = f"报告 {trial.trial_code} 已先签批成为新默认值，本报告需与新默认值重新比较"
    record(db, user.id, "approve_parameter_trial", "ParameterTrial", trial.trial_code,
           {"new_default": {"eps_m": setting.eps_m, "min_samples": setting.min_samples},
            "invalidated_trials": [o.trial_code for o in others]})
    db.commit(); db.refresh(trial)
    return {"trial": trial_view(trial), "cluster_setting": setting_view(setting)}

@app.get("/api/zones")
def zones(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [zone_view(z, db) for z in db.execute(select(RepairZone).order_by(RepairZone.id)).scalars().all()]

@app.post("/api/zones/{zone_id}/split")
def split_zone(zone_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status != "needs_split": raise HTTPException(409, "当前片区没有待拆分的道路边界冲突")
    points = db.execute(select(SurveyPoint).where(SurveyPoint.id.in_(zone.point_ids))).scalars().all()
    notice = db.get(WorkNotice, zone.conflict_notice_id)
    ordered = sorted(points, key=lambda p: (p.x, p.y))
    split_x = (notice.boundary["min_x"] + notice.boundary["max_x"]) / 2 if notice else ordered[len(ordered)//2].x
    left = [p for p in ordered if p.x <= split_x]
    right = [p for p in ordered if p.x > split_x]
    if not left or not right:
        half = max(1, len(ordered)//2); left, right = ordered[:half], ordered[half:]
    zone.status = "split"; zone.split_reason = f"依据 {notice.road_name if notice else '道路边界'} 在 x={round(split_x, 2)} 拆分，保留原始聚类证据"
    children = []
    for idx, members in enumerate([left, right], 1):
        b = bounds([{ "x": p.x, "y": p.y } for p in members])
        child = RepairZone(zone_code=f"{zone.zone_code}-S{idx}", batch_id=zone.batch_id, cluster_label=zone.cluster_label, name=f"{zone.name} / 边界片 {idx}", status="ready_for_notice", point_ids=[p.id for p in members], center_x=round(sum(p.x for p in members)/len(members),2), center_y=round(sum(p.y for p in members)/len(members),2), average_settlement_mm=round(sum(p.settlement_mm for p in members)/len(members),2), max_settlement_mm=max(p.settlement_mm for p in members), local_density=zone.local_density, boundary=b, original_cluster_evidence={"split_from": zone.zone_code, "split_axis": "x", "split_x": split_x, "source_evidence": zone.original_cluster_evidence}, split_from_id=zone.id)
        db.add(child); children.append(child)
    record(db, user.id, "split_zone_by_road_boundary", "RepairZone", zone.zone_code, {"children": [f"{zone.zone_code}-S1", f"{zone.zone_code}-S2"], "split_x": split_x})
    db.commit()
    return {"source_zone": zone.zone_code, "children": [c.zone_code for c in children], "status": zone.status, "split_reason": zone.split_reason}

@app.post("/api/zones/{zone_id}/work-notices")
def create_notice(zone_id: int, payload: NoticeIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status not in ["proposed", "ready_for_notice"]: raise HTTPException(409, "当前片区不能发起施工联动")
    notice = WorkNotice(notice_code=f"WN-{zone.zone_code}-{int(datetime.now().timestamp())}", zone_id=zone.id, road_name=payload.road_name, contractor=payload.contractor, permit_no=payload.permit_no, starts_at=payload.starts_at, ends_at=payload.ends_at, boundary=zone.boundary, status="submitted", lane_closure_required=payload.lane_closure_required, created_by=user.id)
    db.add(notice); zone.status = "notice_sent"; record(db, user.id, "submit_work_notice", "WorkNotice", notice.notice_code, {"zone_code": zone.zone_code}); db.commit(); db.refresh(notice)
    return notice_view(notice)

@app.post("/api/work-notices/{notice_id}/acknowledge")
def acknowledge_notice(notice_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    notice = db.get(WorkNotice, notice_id)
    if not notice: raise HTTPException(404, "施工联动单不存在")
    if notice.status != "submitted": raise HTTPException(409, "施工联动单不在待确认状态")
    notice.status = "active"; record(db, user.id, "acknowledge_work_notice", "WorkNotice", notice.notice_code, {"status": "active"}); db.commit()
    return notice_view(notice)

@app.post("/api/zones/{zone_id}/start-repair")
def start_repair(zone_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    notice = db.execute(select(WorkNotice).where(WorkNotice.zone_id == zone.id, WorkNotice.status == "active")).scalar_one_or_none()
    if not notice: raise HTTPException(409, "施工联动单未激活，不能开始维修")
    zone.status = "repairing"; record(db, user.id, "start_repair", "RepairZone", zone.zone_code, {"notice_code": notice.notice_code}); db.commit()
    return zone_view(zone, db)

@app.post("/api/zones/{zone_id}/resurvey")
def submit_resurvey(zone_id: int, payload: ResurveyIn, db: Session = Depends(get_db), user: User = Depends(require_roles("operator", "supervisor"))):
    zone = db.get(RepairZone, zone_id)
    if not zone: raise HTTPException(404, "维修片区不存在")
    if zone.status != "repairing": raise HTTPException(409, "片区未进入维修中，不能提交复测")
    after = round(payload.settlement_after_mm, 2); before = zone.average_settlement_mm; delta = round(before - after, 2); pass_fail = "pass" if after <= 8.0 else "fail"
    record_code = f"RS-{zone.zone_code}-{int(datetime.now().timestamp())}"
    resurvey = Resurvey(record_code=record_code, zone_id=zone.id, measured_at=payload.measured_at, settlement_before_mm=before, settlement_after_mm=after, delta_mm=delta, pass_fail=pass_fail, status="submitted", notes=payload.notes)
    db.add(resurvey); zone.status = "awaiting_resurvey"; record(db, user.id, "submit_resurvey", "Resurvey", record_code, {"zone_code": zone.zone_code, "pass_fail": pass_fail}); db.commit(); db.refresh(resurvey)
    return resurvey_view(resurvey)

@app.post("/api/resurveys/{resurvey_id}/verify")
def verify_resurvey(resurvey_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor"))):
    resurvey = db.get(Resurvey, resurvey_id)
    if not resurvey: raise HTTPException(404, "复测记录不存在")
    if resurvey.status != "submitted": raise HTTPException(409, "复测记录不在待核验状态")
    zone = db.get(RepairZone, resurvey.zone_id)
    resurvey.verified_by = user.id
    if resurvey.pass_fail == "pass":
        resurvey.status = "verified"; zone.status = "closed"
        notice = db.execute(select(WorkNotice).where(WorkNotice.zone_id == zone.id)).scalar_one_or_none()
        if notice: notice.status = "completed"
    else:
        resurvey.status = "rejected"; zone.status = "repairing"
    record(db, user.id, "verify_resurvey", "Resurvey", resurvey.record_code, {"result": resurvey.pass_fail, "zone_status": zone.status}); db.commit()
    return {"resurvey": resurvey_view(resurvey), "zone": zone_view(zone, db)}

@app.get("/api/audit-logs")
def audit_logs(db: Session = Depends(get_db), user: User = Depends(require_roles("supervisor", "viewer"))):
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(80)).scalars().all()
    return [{"id": r.id, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id, "detail": r.detail, "created_at": iso(r.created_at)} for r in rows]
