"""HID Campus Access - FastAPI + AWS Rekognition.

  Liveness Lab : Rekognition DetectFaces/DetectLabels + on-device checks  -> LIVE / UNCERTAIN / SPOOF
  Campus Kiosk : barcode -> registered card? -> allowed zone + time?       -> ALLOWED / NOT ALLOWED
                 (no face, no spoof check - barcode registry only)

Run: uvicorn main:app --reload --port 8000   (from the backend folder)
"""
import base64, json, os, random, sqlite3, time, uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REGION = os.getenv("AWS_REGION", "ap-south-1")
DDB_TABLE = os.getenv("DDB_TABLE")  # optional: mirror every decision to DynamoDB (partition key attempt_id, String)
TZ = ZoneInfo(os.getenv("CAMPUS_TZ", "Asia/Kolkata"))
EDGE_DB = Path(os.getenv("EDGE_DB", Path(__file__).parent / "edge.db"))

SPOOF_LABEL_CONF = 75.0  # DetectLabels confidence for screen/paper/phone that we treat as a spoof

rek = boto3.client("rekognition", region_name=REGION)
pool = ThreadPoolExecutor(10)
app = FastAPI(title="HID Campus Access")

# ---- campus schedule (zones a card may be allowed into) -----------------------
ZONES = {
    "main_gate": {"start": "06:00", "end": "22:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    "library":   {"start": "08:00", "end": "20:00", "days": [0, 1, 2, 3, 4, 5]},
    "labs":      {"start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]},
    "hostel":    {"start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]},
}
LOG: list[dict] = []      # every decision (lab + kiosk) - mirrored in edge.db
SEEN: set[str] = set()    # attempt_ids already in LOG (so a re-sync never double counts)


# =============================== STORAGE (SQLite) =============================
@contextmanager
def db():
    c = sqlite3.connect(EDGE_DB, timeout=10)
    try:
        c.execute("create table if not exists audit(id text primary key, rec text, synced int default 0)")
        c.execute("create table if not exists students(prn text primary key, name text, zones text)")  # legacy
        c.execute("create table if not exists cards(barcode text primary key, prn text, name text, zones text)")
        yield c
        c.commit()
    finally:
        c.close()


def norm(s: str) -> str:
    return (s or "").strip().upper()


def get_card(barcode: str):
    with db() as c:
        r = c.execute("select barcode,prn,name,zones from cards where barcode=?", (norm(barcode),)).fetchone()
    return {"barcode": r[0], "prn": r[1], "name": r[2], "zones": json.loads(r[3])} if r else None


def upsert_card(barcode: str, prn: str, name: str, zones: list[str]) -> bool:
    """Returns True if an existing card was updated, False if new."""
    with db() as c:
        existed = c.execute("select 1 from cards where barcode=?", (norm(barcode),)).fetchone() is not None
        c.execute("insert or replace into cards values(?,?,?,?)",
                  (norm(barcode), norm(prn), name.strip() or norm(prn), json.dumps([z for z in zones if z in ZONES])))
    return existed


def persist(rec: dict):
    """Every decision goes to the local audit table first, then to DynamoDB when configured + reachable."""
    try:
        with db() as c:
            c.execute("insert or replace into audit(id,rec,synced) values(?,?,0)",
                      (rec.get("attempt_id") or uuid.uuid4().hex, json.dumps(rec)))
    except Exception as e:
        print("audit write failed:", e)
    flush_audit()


def flush_audit():
    if not DDB_TABLE:
        return
    try:
        tbl = boto3.resource("dynamodb", region_name=REGION).Table(DDB_TABLE)
        with db() as c:
            for id_, rec in c.execute("select id,rec from audit where synced=0").fetchall():
                tbl.put_item(Item=json.loads(rec, parse_float=Decimal))
                c.execute("update audit set synced=1 where id=?", (id_,))
    except Exception as e:
        print("DynamoDB sync deferred:", e)


def startup_load():
    with db() as c:
        # old versions kept students(prn,...) - their PRN doubles as the barcode so nobody is locked out
        c.execute("insert or ignore into cards(barcode,prn,name,zones) select prn,prn,name,zones from students")
        # demo card matching the barcode drawn on the page
        c.execute("insert or ignore into cards values('PES1202401983','PES1202401983','Demo Student',?)",
                  (json.dumps(["main_gate", "library", "labs"]),))
        for (rec,) in c.execute("select rec from audit order by rowid"):
            r = json.loads(rec)
            LOG.append(r)
            SEEN.add(r.get("attempt_id", ""))


startup_load()


# =============================== SCHEDULE ====================================
def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def access_check(card, zone: str):
    """(allowed, reason) for a registered card at a zone right now."""
    z = ZONES.get(zone)
    if not z:
        return False, "Unknown door"
    if zone not in card["zones"]:
        return False, f"Not authorised for {zone.replace('_', ' ')}"
    now = datetime.now(TZ)
    t = now.hour * 60 + now.minute
    if now.weekday() in z["days"] and minutes(z["start"]) <= t <= minutes(z["end"]):
        return True, "Registered card, within allowed hours"
    return False, f"Outside allowed hours for {zone.replace('_', ' ')} ({z['start']}-{z['end']})"


# =============================== KIOSK API ===================================
class Register(BaseModel):
    barcode: str
    prn: str
    name: str = ""
    zones: list[str] = ["main_gate", "library"]


@app.post("/api/register")
def register(r: Register):
    if not norm(r.barcode) or not norm(r.prn):
        raise HTTPException(422, "Barcode and PRN are both required")
    updated = upsert_card(r.barcode, r.prn, r.name, r.zones)
    return {"registered": norm(r.barcode), "prn": norm(r.prn), "updated": updated}


class Access(BaseModel):
    barcode: str
    zone: str = "main_gate"


@app.post("/api/access")
def access(a: Access):
    t0 = time.perf_counter()
    card = get_card(a.barcode)
    if not card:
        ok, why = False, "Card not registered"
    else:
        ok, why = access_check(card, a.zone)
    out = {"attempt_id": uuid.uuid4().hex[:10], "kind": "kiosk", "barcode": norm(a.barcode),
           "prn": card["prn"] if card else None, "name": card["name"] if card else None, "zone": a.zone,
           "decision": "ALLOWED" if ok else "NOT_ALLOWED", "reason": why,
           "ts": datetime.now(TZ).isoformat(timespec="seconds"), "offline": False}
    out["time_ms"] = int((time.perf_counter() - t0) * 1000)
    LOG.append(out)
    SEEN.add(out["attempt_id"])
    pool.submit(persist, out)
    return out


@app.get("/api/policy")  # the kiosk caches this for offline decisions
def policy():
    with db() as c:
        rows = c.execute("select barcode,prn,name,zones from cards order by rowid").fetchall()
    return {"zones": ZONES, "cards": {b: {"prn": p, "name": n, "zones": json.loads(z)} for b, p, n, z in rows}}


# =============================== LIVENESS LAB ================================
LABEL_GROUPS = {
    "printed": {"Paper", "Page", "Poster", "Flyer", "Brochure", "Photo Frame", "Photography", "Book", "Text"},
    "screen": {"Screen", "Monitor", "Display", "LCD Screen", "Television", "Computer Hardware", "Electronics"},
    "phone": {"Mobile Phone", "Phone", "Cell Phone", "Tablet Computer", "Iphone"},
    "mask": {"Mask", "Costume", "Doll", "Statue", "Sculpture", "Mannequin", "Toy"},
}


def dec(b64: str) -> bytes:
    return base64.b64decode(b64.split(",")[-1])


def r_detect(img: bytes):
    return rek.detect_faces(Image={"Bytes": img}, Attributes=["ALL"])["FaceDetails"]


def r_labels(img: bytes):
    return rek.detect_labels(Image={"Bytes": img}, MaxLabels=40, MinConfidence=55)["Labels"]


def area(f) -> float:
    b = f["BoundingBox"]
    return b["Width"] * b["Height"]


def dominant(faces):
    """The person at the kiosk = the largest face. Tiny background faces are ignored instead of failing the run.
    Returns a face dict, None (no face) or 'multi' (two similar-sized faces)."""
    if not faces:
        return None
    big = max(faces, key=area)
    if any(f is not big and area(f) > 0.45 * area(big) for f in faces):
        return "multi"
    return big


class Analyze(BaseModel):
    frames: list[str]  # up to 6 frames captured over ~2 s


@app.post("/api/analyze")
def analyze(a: Analyze):
    """Raw Rekognition signals over several frames; the browser fuses them with on-device checks."""
    if len(a.frames) < 2:
        raise HTTPException(400, "Need at least 2 frames")
    imgs = [dec(f) for f in a.frames[:6]]
    try:
        futs = [pool.submit(r_detect, i) for i in imgs]
        lab_futs = [pool.submit(r_labels, imgs[0]), pool.submit(r_labels, imgs[len(imgs) // 2])]
        doms = [dominant(f.result()) for f in futs]
        labels = [l for lf in lab_futs for l in lf.result()]
    except Exception as e:
        raise HTTPException(502, f"Rekognition error: {e}")

    groups = {g: max([l["Confidence"] for l in labels if l["Name"] in names] or [0]) for g, names in LABEL_GROUPS.items()}
    ok = [d for d in doms if isinstance(d, dict)]
    multi = sum(d == "multi" for d in doms)
    none = sum(d is None for d in doms)

    hint = None
    if len(ok) < 2:
        if multi >= max(1, len(doms) // 2):
            hint = "More than one face is in view. Only one person at a time."
        elif none >= len(doms) - 1:
            hint = "No face found. Face the camera with good light on your face."
        else:
            hint = "Face was lost in several frames. Stay still inside the frame and retry."
    elif sorted(area(d) for d in ok)[len(ok) // 2] < 0.04:
        hint = "Move closer to the camera so your face fills the frame."
        ok = []
    if len(ok) < 2:
        return {"face_ok": False, "labels": groups, "frames_ok": len(ok), "hint": hint}

    avg = lambda k: sum(f["Quality"][k] for f in ok) / len(ok)
    if avg("Brightness") < 20:
        return {"face_ok": False, "labels": groups, "frames_ok": len(ok), "hint": "Too dark. Add light in front of your face."}

    yaw = [f["Pose"]["Yaw"] for f in ok]
    pitch = [f["Pose"]["Pitch"] for f in ok]
    eyes = [(f["EyesOpen"]["Value"], f["EyesOpen"]["Confidence"]) for f in ok if f["EyesOpen"]["Confidence"] > 50]
    has_open = any(v for v, _ in eyes)
    has_closed = any(not v for v, _ in eyes)
    low_conf = any(v and c < 85 for v, c in eyes)  # mid-blink frames read as "open, but unsure"
    blink = has_open and (has_closed or low_conf)

    return {
        "face_ok": True, "frames_ok": len(ok), "labels": groups,
        "yaw_range": round(max(yaw) - min(yaw), 1), "pitch_range": round(max(pitch) - min(pitch), 1),
        "blink": blink, "blink_detail": {"has_open": has_open, "has_closed": has_closed, "low_conf": low_conf},
        "mouth_change": len({f["MouthOpen"]["Value"] for f in ok}) > 1,
        "smile_change": len({f["Smile"]["Value"] for f in ok}) > 1,
        "brightness": round(avg("Brightness"), 1), "sharpness": round(avg("Sharpness"), 1),
        "confidence": round(sum(f["Confidence"] for f in ok) / len(ok), 1),
    }


@app.get("/api/challenge")
def new_challenge():
    return {"challenge": random.choice(["turn", "blink", "smile"])}


# =============================== SYNC / REPORT ===============================
class Sync(BaseModel):
    records: list[dict]


@app.post("/api/sync")
def sync(s: Sync):
    """Receives lab results, offline kiosk decisions and offline card registrations."""
    n = 0
    for r in s.records:
        if r.get("kind") == "register":
            upsert_card(r.get("barcode", ""), r.get("prn", ""), r.get("name", ""), r.get("zones", []))
            n += 1
            continue
        aid = r.setdefault("attempt_id", uuid.uuid4().hex[:10])
        if aid in SEEN:
            continue
        SEEN.add(aid)
        r["synced_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        LOG.append(r)
        persist(r)
        n += 1
    return {"synced": n}


@app.get("/api/report")
def report():
    """Performance metrics come from Liveness Lab runs that carry a ground-truth label (live/printed/screen/video/mask).
    Kiosk decisions are counted separately - the kiosk has no biometric to score."""
    lab = [r for r in LOG if r.get("zone") == "lab"]
    tr = [r for r in lab if r.get("truth")]
    rate = lambda a, b: round(a / b, 4) if b else None

    def correct(r):  # real person accepted, or attack NOT accepted
        return bool(r.get("live_ok")) if r["truth"] == "live" else not r.get("live_ok")

    att = [r for r in tr if r["truth"] != "live"]
    gen = [r for r in tr if r["truth"] == "live"]
    by_truth: dict = {}
    for r in tr:
        d = by_truth.setdefault(r["truth"], {"n": 0, "correct": 0})
        d["n"] += 1
        d["correct"] += correct(r)

    times = sorted(r["time_ms"] for r in lab if r.get("time_ms") is not None)
    decisions = {"ALLOW": 0, "STEP_UP": 0, "DENY": 0}
    for r in lab:
        decisions[r.get("decision", "DENY")] = decisions.get(r.get("decision", "DENY"), 0) + 1
    kiosk = [r for r in LOG if r.get("kind") == "kiosk" or r.get("decision") in ("ALLOWED", "NOT_ALLOWED")]

    return {
        "n_total": len(lab), "n_scored": len(tr), "decisions": decisions, "by_truth": by_truth,
        "n_live": len(tr), "liveness_acc": rate(sum(correct(r) for r in tr), len(tr)),
        "n_att": len(att), "far": rate(sum(bool(r.get("bio_ok")) for r in att), len(att)),
        "n_gen": len(gen), "frr": rate(sum(not r.get("bio_ok", False) for r in gen), len(gen)),
        "avg_ms": round(sum(times) / len(times)) if times else None,
        "within_2s": rate(sum(t <= 2000 for t in times), len(times)),
        "kiosk": {"allowed": sum(r.get("decision") == "ALLOWED" for r in kiosk),
                  "not_allowed": sum(r.get("decision") == "NOT_ALLOWED" for r in kiosk)},
        "recent": list(reversed(LOG[-200:])),
    }


@app.get("/api/audit/status")
def audit_status():
    with db() as c:
        return {"pending": c.execute("select count(*) from audit where synced=0").fetchone()[0]}


@app.post("/api/reset")
def reset():
    LOG.clear()
    SEEN.clear()
    with db() as c:
        c.execute("delete from audit")
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")