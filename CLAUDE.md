# CLAUDE.md — Smart kitchen scale with camera

## What we are building
A cheap kitchen scale with a built-in camera that logs food without touching the phone:
put food on the scale → weight stabilizes → camera takes one photo → server recognizes
the item (or reads its barcode) → top-3 options shown on the scale's OLED → user confirms
with buttons → entry goes to the diary. The phone app is only for stats, rare corrections
and photographing labels of unknown products.

- Market: Russia. Target retail 2,990–3,490 RUB (below ~2,580 RUB every unit loses money on marketplaces).
- Target user: people who already weigh food (cutting/bulking, athletes, clients of coaches/nutritionists).
- Differentiator vs RU competitors (Futula ~1.2–2k RUB, Ultrean K40H ~2.2–3.5k RUB — Bluetooth scales,
  food picked in the phone app): camera in the scale, no phone needed, Russian product DB and UI.
- Closest global analogue: Qalzy (UK, £199+, camera scale, not targeting Russia).

## Key decisions
- **No voice input.** Correction = 3 buttons (up / down / OK) over top-3 list; last item "other" → fix in phone.
- **No battery.** USB-C powered (cheaper, fewer returns, no lithium shipping issues).
- **No on-device ML.** Device only weighs, shoots, sends; all recognition on our server (models can be updated daily).
- **Photos are not stored** after recognition unless the user consents (152-FZ, cost).
- **Barcodes are decoded deterministically** (zxing-cpp) on every frame first; the classifier's "barcode/package"
  class is only a routing signal ("bring the code closer"), never the way to read digits.

## Recognition stack (server)
1. Barcode decoder (EAN-13/8, UPC, Chestny Znak DataMatrix → GTIN) → own RU product DB.
2. Own image classifier for 300–500 most common foods (EfficientNet B0–B3 / EfficientNet-Lite / MobileNetV3),
   fine-tuned at home on RTX 5060, served on CPU via ONNX Runtime (INT8 later if accuracy holds).
3. Open-vocabulary embedding model (CLIP/SigLIP) on our server for items outside the class list.
4. Large VLM only when 1–3 are unsure (~15% of frames): Qwen2.5-VL 7B via Yandex AI Studio (~0.17 RUB/frame).
   Planned: fine-tune a VLM on our labeled data.
5. Per-user memory: kNN over embeddings of the user's own corrected photos.
- Camera cannot tell: fat % (cottage cheese 5% vs 9%), raw vs cooked, chicken vs turkey, sugar vs salt →
  ask within the group and remember the user's last choice.
- Budget: ≤150–200 ms per model call on 1 vCPU; whole response ≤ ~2 s incl. Wi-Fi.
- Server cost target: 5–10 RUB / active user / month; VPS in Russia 200–600 RUB/month.

## Hardware (prototype)
- ESP32-CAM (OV2640, ≤2 MP), later ESP32-S3-WROOM-1-N8R8 + OV2640 on own PCB.
- 5 kg single-point (bar) load cell, e.g. TAL220/YZC-131, or salvaged from an ~800 RUB kitchen scale.
- ADC: NAU7802 (I2C, shares bus with OLED — ESP32-CAM is short on GPIO) or HX711 (10 Hz mode).
- 0.96" OLED, 3 buttons, LED illumination, stable 5 V / 1 A supply.
- Firmware flow: stable weight (σ < 0.5 g over 1 s, Δ > 2 g) → photo → re-check weight → POST multipart
  (JPEG + JSON: device_id, seq, weight_g, delta_g, tare_g, stable, ts) → server returns top-3 with seq →
  user choice sent back with same seq (becomes a labeled training sample).
- Production idea: ready-made scale platform from a Chinese factory + own PCB + one custom part (camera post).
  BOM ≈ $12 FOB, ≈ 1,370 RUB landed (incl. 22% import VAT). One-off costs ≈ 350k RUB.

## Data pipeline (scripts)
User-facing guide (RU): `DATA_PIPELINE.md` — keep it in sync when the pipeline changes.
- `build_dataset.py` — the only entry point, no args. `custom_dataset/raw/<class>/` (class = folder; user's photos,
  never modified) → `dataset/images/<class>/*_ov{0,1,2}.jpg` + `labels.csv` (row = object: file, source, class,
  barcode, x1..y2 on the copy, readable, checked) + `params.json`. No train/test split here — the user splits with
  sklearn grouped by `source`. Food has no boxes (whole-frame classifier; label = folder).
- `custom_dataset/esp32/<class>/esp_*.jpg` — real ESP32-CAM shots (UXGA, flash) from
  `scale_controller/image_handler/shoot.py`; not degraded, not read by `build_dataset.py` (real test set / sim calibration).
- Barcodes (class `barcodes` only): value + box from zxing-cpp on the original, every photo reviewed once in CVAT
  in its native "CVAT for images 1.1" XML both ways: script writes `cvat/upload.zip` + `cvat/prelabels.xml`,
  user drops CVAT export zips into `cvat/verified/` as downloaded. Boxes go to copies via `map_box()`.
- `export_onnx.py` + `bench_latency.py` — EfficientNet B0–B4 → ONNX FP32/INT8 (static QDQ), latency on 1 thread;
  bench needs only onnxruntime+numpy (copy `onnx_models/` to the VPS). M1, 1 thread, p50 FP32/INT8 ms:
  B0 26/10, B1 40/18, B2 47/21, B3 84/36, B4 144/62 (cheap x86 vCPU expected 2–3× slower).
- `train.py b0 … b4 [--epochs N]` — same recipe for each (timm pretrained, AdamW+OneCycle, label smoothing),
  split as in DATA_PIPELINE §7, extra test on real `esp32/` shots, best by val top-3 → `runs/<model>/` + `runs/results.csv`.
- `ov2640_sim.py` — `degrade()`: blur, distortion, WB errors, low DR, vignetting, signal-dependent + chroma noise,
  SVGA/XGA/UXGA, JPEG q35–75, sometimes double JPEG; params incl. 4:3 crop logged. `map_box()` uses the same
  geometry. Calibrate ranges against real ESP32-CAM shots when the chip arrives.

## Roadmap (with go/stop gates)
1. **Now (no camera yet):** class list (50–100) with KBZhU per 100 g; shoot own food on iPhone in scale geometry
   (top-down/45°, 25–30 cm, ultra-wide for part); degrade; hold out test set; benchmark B0–B3 latency on a
   cheap VPS (ONNX, FP32/INT8); linear probe; try CLIP/SigLIP zero-shot with RU labels; FastAPI `/scan` +
   Python device emulator; weight logic on any ESP32 if load cell arrives first.
2. **Prototype (chip arrives):** ESP32-CAM + load cell + OLED + 3D-printed post; calibrate simulator; fine-tune
   1–2 finalist classifiers; measure on 200 own meals.
   Gate: correct item in top-3 ≥ 90%, scan ≤ 3 s, barcodes readable from 15–20 cm.
3. **Demand check:** landing page + waitlist + 1,000 RUB deposits; 20–30 interviews; 3–5 coaches.
   Gate: 100+ waitlist, 20 people logging 2+ weeks.
4. **Small batch 30–50 units**, hand-assembled, sold at full price with prepayment.
   Gate: 50+ paid preorders, returns < 10%, 30%+ take premium.
5. **Production:** factory quotes (fail if FOB > $15), camera-post mold, EAEU certification, batch 500–1,000,
   marketplaces + coaches channel.

## Open questions / risks
- Technological fee on electronics from 1 Dec 2026 (fixed per unit, up to 5,000 RUB, rates TBD) — could kill margin.
- Marketplace commission for "kitchen scales" category (assumed 20%), customs code and duty.
- Subscription payments in RU: Google Play billing suspended, App Store doesn't take RU cards → sell via own site + RuStore.
- 152-FZ: data stored in Russia, RKN notification, diet data may count as health data.
- Retention in calorie tracking is low (fitness apps ~3% D30) — device helps only people already weighing food.

# Behavioral guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.