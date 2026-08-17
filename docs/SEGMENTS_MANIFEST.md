# A001_C004 Overlapped Segments — Manifest (2026-08-16)

Source: `Thomas_A001_C004_STATIC_FULL.mp4` (1328x1080 @ 30fps, static window @ plate x=479, 1831 frames).
Segment starts chosen at sharp, non-blurred frames with **both hands visible** (verified frame by frame).
Overlaps are 60 frames (2.0s) of shared content for blending in Resolve.

| Segment | Driver frames | Start TC | Length | Start pose (hands) | Budget @30fps | Budget @25fps |
|---|---|---|---|---|---|---|
| SEG01 | 0 – 454 | 0.0s | 15.2s | standing, fists at sides | 455 | 380 |
| SEG02 | 395 – 785 | 13.2s | 13.0s | arms out, fingers spread | 391 | 326 |
| SEG03 | 726 – 1248 | 24.2s | 17.4s | standing, arms hanging | 523 | 436 |
| SEG04 | 1189 – 1544 | 39.6s | 11.9s | both hands on hips | 356 | 297 |
| SEG05 | 1485 – 1830 | 49.5s | 11.5s | hand on hip + hand at side | 346 | 289 |

Blend zones (shared frames, driver timeline): 395-454 · 726-785 · 1189-1248 · 1485-1544.
The 42s full-stage transit (frames ~1260-1430) sits entirely inside SEG04 — no cut interrupts it.

## Per-segment files

- `Thomas_A001_C004_SEGnn.mp4` — driving video (CRF 12, audio included)
- `Thomas_A001_C004_SEGnn_startframe.png` — 1328x1080 first frame, the pose reference for generating MJ's
  first-frame anchor image. Generate the MJ version at the same framing/aspect (ideally at the SCAIL output
  resolution below) so the anchor doesn't fight the driver.

## Generation settings

- Source stays 30fps — do NOT pre-convert; `force_rate` handles sampling.
  Prefer generating at 30fps (test on SEG01; segment lengths leave headroom); fallback `force_rate` 25.
- SCAIL output resolution (mod-64, matches 1328:1080 within 0.2%): **1728x1408** (2.43 MP).
  Lighter fallback: 1600x1280 (2.05 MP).
- `start_time` = 0. Frame budget per the table (use the column matching your force_rate).
- Placement downstream: constant offset (static window), 1x geometry — final MJ is ~1000px tall on the 4K plate.

## Resolve assembly

- Align segments by driver timecode (start TC column); each overlap gives a 2s window to place the blend.
- MJ appearance may differ slightly between segments (independent generations): prefer placing the
  cut/blend where motion is fastest within each overlap zone.
