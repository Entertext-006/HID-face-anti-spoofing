# 🔐 HID Campus Access — AI-Powered Face Liveness & Smart Campus Security

**An intelligent campus access management prototype combining AI-powered face liveness detection, barcode-based student authentication, and offline-first access control.**

Built for the HID Hackathon.

---

## 🌟 Overview

HID Campus Access is a smart campus security system designed to improve identity verification and control entry into different campus zones.

The platform combines two independent modules:

1. **Liveness Lab:** Evaluates whether a camera view appears to show a real person or a presentation attack, such as a printed photograph, phone screen, recorded video, or mask.
2. **Campus Kiosk:** Uses student ID barcodes to verify registration and determine whether a student is authorized to enter a particular campus zone.

The system also supports offline kiosk operations, local data persistence, audit logging, and synchronization when connectivity returns.

## ✨ Key Features

### 1. AI-Powered Face Liveness Detection
- Captures multiple camera frames over approximately two seconds.
- Uses AWS Rekognition `DetectFaces` and `DetectLabels`.
- Combines cloud-based signals with browser-side checks.
- Evaluates potential presentation attacks.
- Produces LIVE, UNCERTAIN, or SPOOF outcomes.

### 2. Barcode-Based Student Authentication
- Scans student ID barcodes using the device camera.
- Supports manual barcode entry.
- Registers students using barcode, PRN, name, and authorized zones.
- Checks student registration before granting access.

### 3. Zone-Based Access Control
- Controls access to different campus locations.
- Applies zone-specific authorization rules.
- Validates permitted days and time windows.
- Displays ALLOWED or NOT ALLOWED with an explanation.

### 4. Offline-First Functionality
- Caches student records and campus access policies.
- Supports offline barcode verification.
- Stores offline registrations and access decisions locally.
- Synchronizes queued records when internet connectivity returns.
- Uses browser-side liveness checks in reduced offline mode.

### 5. Audit Logging & Performance Evaluation
- Records liveness and campus
