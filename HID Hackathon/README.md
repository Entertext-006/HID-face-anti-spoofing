# HID Campus Access

Two screens, one backend:

| Screen | What it does | Online | Offline |
|---|---|---|---|
| **Liveness Lab** | Real person or spoof? (printed photo / screen / video / mask) | Rekognition `DetectFaces` + `DetectLabels` on 6 frames, fused with on-device checks | On-device checks only (needs a few online "real person" runs first so the device learns your camera) |
| **Campus Kiosk** | Scan barcode → **ALLOWED / NOT ALLOWED**. No face, no spoof check. | Server looks the barcode up in `edge.db` | Uses the card list cached in the browser; results + new registrations queue and sync later |

## Structure
```
hid-liveness/
├── README.md
├── backend/
│   ├── main.py            # FastAPI app
│   ├── requirements.txt
│   └── edge.db            # created automatically (SQLite)
└── static/
    ├── index.html         # whole frontend, served by FastAPI
    ├── sw.js              # service worker: lets the page open offline
    └── vendor/zxing.min.js  # OPTIONAL but needed for offline barcode scanning on Firefox/Safari/Windows Chrome
```
Get `zxing.min.js` once: https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js → save as `static/vendor/zxing.min.js`.

## Run
```
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export AWS_REGION=ap-south-1 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...
# optional: DDB_TABLE=<table, partition key attempt_id (String)>
uvicorn main:app --reload --port 8000
```
Open http://localhost:8000 (camera needs localhost or HTTPS). After updating, hard-refresh (Ctrl+Shift+R) once so the new service worker takes over.

IAM: `rekognition:DetectFaces`, `rekognition:DetectLabels` (+ `dynamodb:PutItem` if DDB_TABLE is set). Collections/IndexFaces are no longer used.

## Campus Kiosk – how to use
1. **Register card** tab → show the barcode to the camera (or type it) → enter PRN (+ name, doors) → **Register**.
2. **Entry** tab → pick the door → show the barcode → **ALLOWED** or **NOT ALLOWED** with the reason
   (card not registered / not authorised for that door / outside hours).
3. Click the **Online** pill (top right) to simulate losing the connection. Scan, register and test as above, then click it again; pending items sync.

## Performance evaluation – how to use
Bottom bar (Liveness Lab only): choose what you are showing the camera (Real person, Printed photo, Screen, Video, Mask), then press **Check liveness**. Every run is scored against that choice.
* Liveness accuracy = real accepted + attacks not accepted, over all scored runs
* FAR = attacks that were accepted / all attacks. FRR = real people rejected / all real people
* Run each attack type many times; 0.2% FAR needs ~500+ attack trials to mean anything.

## Where data is stored
* `backend/edge.db` (SQLite): `cards` (barcode→PRN registry), `audit` (every lab + kiosk decision), `students` (legacy, auto-migrated into `cards`)
* DynamoDB (optional, `DDB_TABLE`): mirror of `audit`
* Browser localStorage: cached card list/schedule, offline queue, learned "real face" baseline
* Rekognition stores nothing for this app now (no face collection)