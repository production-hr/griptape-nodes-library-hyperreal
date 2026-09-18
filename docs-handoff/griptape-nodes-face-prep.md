---
title: Face Prep Nodes
section: Pipeline
subsection: Griptape Nodes
category: Face Prep
excerpt: Detect, crop, and composite head regions around external face-swap and lipsync passes.
tags: [ai, video, face-swap, pipeline, workflow, reference]
updated: 2026-09-18
---
> lede: Five local nodes that cut a stable head region out of a plate, hand it to an external swap or lipsync step, and paste the result back pixel-exact.

The Face Prep nodes exist because of resolution arithmetic. A face-swap or lipsync model working on a full 1080p frame sees a head that occupies a small fraction of its input; the same model working on a 512-1024 px head crop sees ten times the pixels where it matters. These nodes bracket that external step — a ComfyUI face swap, a HeyGen lipsync, a Topaz upscale in between — so the expensive generation runs on a tight crop and the finished result lands back on the original plate at exactly the right position and frame count. Three video nodes form the moving-image round trip (Detect Head Region, Crop To Region, Composite Region Back); two image nodes do the same job for stills (Zoom To Head, Composite Face Back). All five run entirely on the local machine using OpenCV, PIL, and ffmpeg — no API, no secrets — and the first run of any video node downloads the ffmpeg binaries once via `static-ffmpeg`. Every node in this set reports through the standard success/failure outputs, with a human-readable summary in `result_details`.

The canonical video chain is:

> Load Video → Detect Head Region → Crop To Region → [Topaz upscale → external face swap] → Composite Region Back → Save Video

## 1. The Region Contract

What makes the round trip invertible is a single JSON dict, schema `hyperreal.head_region/1`, that Detect Head Region emits and both Crop To Region and Composite Region Back validate on input. Any input that is not a dict carrying `"schema": "hyperreal.head_region/1"` is rejected immediately with an error telling you to connect the detect node's output. The contract records everything the downstream nodes need to reproduce the crop exactly:

```json
{
  "schema": "hyperreal.head_region/1",
  "source":   { "width": 1920, "height": 1080, "frame_rate": 25.0, "frame_count": 250 },
  "box":      { "x": 704, "y": 96, "width": 512, "height": 512, "confidence": 0.94 },
  "mode":     "static",
  "offsets":  [[704, 96], [705, 96], "..."],
  "detector": "yunet-2023mar",
  "notes":    { "drift_px": 12, "detection_interval": 5, "frames_missed": 0,
                "clamped": false, "metadata_frame_count": 250, "ffprobe_frame_count": 250 }
}
```

Two properties of the contract carry the whole design. First, the box size is constant for the entire clip — only the position moves, via `offsets`, a list of per-frame integer top-left origins whose length equals `frame_count`. Because the offsets are integers and the size never changes, the crop is a pure pixel copy with no resampling, and the paste-back needs no inverse scaling. Second, `source` records the plate's dimensions, frame rate, and decoded frame count, which is what lets Composite Region Back refuse a mismatched insert before wasting a render. The `box` uses flat `x`/`y`/`width`/`height` keys so stock nodes can consume it, and `notes` carries diagnostics: measured drift in pixels, the sampling interval used, how many sampled frames missed a face, whether the box had to be clamped to the frame, and both the container-metadata and ffprobe packet counts (the decoded count is authoritative; container metadata is unreliable on variable-frame-rate sources).

The same contract is emitted by Detect Figure Track in the Figure Prep set, which is why Crop To Region serves both pipelines unchanged.

## 2. Detect Head Region

Detect Head Region finds the head in a talking-head plate and emits the region contract. Detection uses a vendored YuNet ONNX model (`yunet-2023mar`, shipped inside the library at `hyperreal/faceprep/models/` — nothing downloads at run time, and the node fails before running if the weights file is missing). Rather than detecting on every frame, it samples every `detection_interval` frames and linearly interpolates the head center between samples; per-frame positions are then smoothed with a `smoothing_window`-frame moving average and clamped so the box never leaves the frame. The face box becomes a head box by padding: `pad_top` (largest by default, for hair), `pad_bottom`, and `pad_sides`, each a fraction of the detected face size. Only if you zero out all three pads does the simpler `head_scale` multiplier take over. The box is squared to the largest padded extent seen anywhere in the clip, then snapped up to a multiple of `snap_multiple` (64 by default, which keeps downstream models and encoders happy).

`box_mode` decides whether the box moves. In `tracked` mode every frame gets its own offset; in `static` mode a single origin — the union of all tracked positions, clamped into frame — is used for the whole clip. The default `auto` measures actual drift and picks `static` when the union of positions is no more than 1.15x the box's own area, i.e. when the subject barely moves. Static is the mode you want when you can get it: one fixed box means the external tool sees a rock-steady crop. Multi-subject plates are handled by `subject_index`: the first detection is ranked by confidence (0 = highest), and after that first hit the subject is followed by nearest centroid rather than re-ranked, so a two-person shot cannot swap subjects mid-clip. If some sampled frames find no face, the node interpolates across the gaps and says so in `result_details`; if no face is found in any sampled frame, it fails with the sample count and threshold in the message. A clamped box also produces a warning — it means the head leaves frame at some point.

| Parameter | Default | Purpose |
|---|---|---|
| `video` | — | The plate video to analyze. |
| `detection_interval` | 5 | Run the detector every Nth frame and interpolate between samples. 5 is the field default; raise it for speed on long, calm plates. |
| `confidence_threshold` | 0.6 | Minimum detector confidence for a face to count. Lower it if detection misses. |
| `subject_index` | 0 | Which confidence-ranked face to follow on first detection (0 = highest). |
| `box_mode` | auto | `static` = one fixed box, `tracked` = per-frame offsets, `auto` decides from measured drift. |
| `pad_top` | 0.9 | Padding above the face as a fraction of face height (most of it, for hair). |
| `pad_bottom` | 0.5 | Padding below the face as a fraction of face height. |
| `pad_sides` | 0.35 | Padding each side as a fraction of face width. |
| `head_scale` | 1.6 | Face-to-head expansion factor, used only when all three pads are 0. |
| `smoothing_window` | 9 | Moving-average window (frames) over the tracked position. |
| `snap_multiple` | 64 | Final box dimensions are snapped up to a multiple of this. |

Outputs: `region` (the contract dict), flat `x`/`y`/`width`/`height` integers that can be wired straight into the stock Crop Video node, and `preview_video` — the plate with the detected box drawn on every frame. Always eyeball the preview before committing to a long external render; it is the cheapest place to catch a bad detection.

## 3. Crop To Region

Crop To Region cuts the detected box out of the plate as a standalone clip — the swap intermediate. Feed it the same plate Detect Head Region analyzed plus the region dict. In `static` mode the crop is a single ffmpeg `crop` filter pass; in `tracked` mode the node streams raw frames through an ffmpeg pipe and slices each one with numpy at that frame's offset, so nothing is resampled and nothing is held in memory. Either way the default encode is deliberately near-lossless: CRF 12, `libx264` preset slow. This clip is an intermediate that will be processed and then composited back, not a deliverable, so generation loss here is loss you pay twice. `pix_fmt` offers `yuv444p` to preserve chroma into the swap, but High 4:4:4 Predictive profile chokes some downstream tools — try `yuv420p` first.

The audio handling exists because many face-swap and lipsync tools refuse video without an audio track. `audio_source` defaults to `plate`, which copies the plate's audio onto the crop and falls back to muxing a silent stereo track when the plate has none — so by default the head clip always carries audio. `silent` always adds a silent track; `none` strips audio entirely. If the tracked crop decodes a different number of frames than the region's recorded `frame_count`, a warning is logged, which is your earliest signal that the plate and region no longer belong together.

| Parameter | Default | Purpose |
|---|---|---|
| `video` | — | The plate video (the same one Detect Head Region analyzed). |
| `region` | — | The `hyperreal.head_region/1` dict from Detect Head Region. |
| `crf` | 12 | x264 CRF; near-lossless on purpose, since this is a swap intermediate. |
| `pix_fmt` | yuv420p | `yuv420p` or `yuv444p`. |
| `audio_source` | plate | `plate` (copies plate audio, silent fallback), `silent`, or `none`. |
| `output_directory` | "" | Optional folder to also save the clip into, e.g. `{project_dir}/outputs`. |

Outputs: `head_video` (the square head clip) and `region_out`, the region dict passed through unchanged so it can be wired forward to Composite Region Back without a long cable back to the detect node.

## 4. Composite Region Back

Composite Region Back pastes the externally processed head clip — swapped, and typically upscaled — back onto the original plate at each frame's recorded offset. Its defining behavior is that it validates before doing any work. The plate's dimensions must match the region's recorded source dimensions (a mismatch means the wrong plate was wired in), the insert's frame rate must match the plate's within 0.01 fps, and the insert's frame count must exactly equal the region's `frame_count`. A frame-count mismatch fails immediately, naming both counts, because a composite that starts one frame off desyncs progressively and looks exactly like a tracking bug — a failure that is far cheaper to read in `result_details` than to diagnose in a render.

> **Warning:** The processed clip must come back with exactly the frame count the region records. The most common way to break this is enabling frame interpolation in a Topaz upscale between the crop and the composite — the node will (correctly) refuse the insert. Upscale only; never retime the head clip.

The insert does not need to come back at box resolution: an upscaled insert is Lanczos-downscaled to the box size on the way in (downscaling from the upscaled resolution is the safe resampling direction, and is precisely how the crop-upscale-swap round trip gains sharpness). A non-square insert is accepted with a logged warning and resized. The blend edge is a procedural matte — `ellipse` (default) or `rounded_rect` — that is inset, eroded by `mask_shrink_px` so the edge sits inside the detected region, then Gaussian-feathered by `feather_px`. Wiring a `mask_video` (a white-on-black matte at insert resolution, e.g. from a SAM video node) replaces the procedural shape per frame, with the same shrink and feather applied. `color_match` performs a per-frame mean/std transfer in LAB from the plate region onto the insert; it is off by default for video, where the swap usually inherits the plate's grade. Audio comes from the plate by stream copy (default) or can be omitted; a plate without audio yields a silent composite with a logged warning.

| Parameter | Default | Purpose |
|---|---|---|
| `plate_video` | — | The original plate. |
| `insert_video` | — | The processed head clip to paste back. |
| `region` | — | The region dict from Detect Head Region or Crop To Region's pass-through. |
| `mask_video` | — | Optional white-on-black matte at insert resolution; overrides `edge_shape`. |
| `edge_shape` | ellipse | Procedural matte shape (`ellipse` or `rounded_rect`). |
| `feather_px` | 24 | Feather width in pixels at box scale. |
| `mask_shrink_px` | 8 | Erode the matte inward before feathering. |
| `color_match` | false | Per-frame LAB mean/std match of the insert to the plate region. |
| `audio_source` | plate | `plate` (copy the plate's audio) or `none`. |
| `crf` | 16 | x264 CRF for the composited output. |
| `output_directory` | "" | Optional folder to also save the composite into. |

Output: `composited_video`, the plate with the insert blended back, plus the plate's audio when present.

## 5. Zoom To Head

Zoom To Head is the still-image entry point: it crops a head-and-shoulders framing out of a full-body image so a lipsync or enhancement pass runs on a face with many more pixels, and its result can later be blended back over the full-body render. It detects the face with the same vendored YuNet model, then pads by `pad_top`, `pad_bottom`, and `pad_sides` as fractions of the face size — the defaults (1.2 / 2.6 / 1.4) reach roughly mid-chest for a head-and-shoulders framing rather than the tight head box the video node produces. The crop then grows (never shrinks) to the target `aspect_ratio` so nothing the pads asked for is lost, and is clamped into frame. `source`, the default aspect, keeps the input image's own shape, which is usually right: the lipsync render comes back the same shape as the full-body one.

Leave `output_long_edge` at 0 unless you have a reason not to: 0 keeps the crop's native pixels with no resampling, which is the best source you can hand an upscaler. The node measures a `zoom_factor` — how much larger the face is in the crop than in the source framing — and warns in `result_details` when it lands under 2.0, because a sub-2x zoom is not enough gain to justify the second pass. A group shot is handled by `subject_index` over confidence-ranked faces; asking for an index beyond the number of faces found is a hard failure, as is finding no face at all.

| Parameter | Default | Purpose |
|---|---|---|
| `image` | — | The full-body source image. |
| `pad_top` | 1.2 | Headroom above the face, as a fraction of face height. Raise for big hair or a hat. |
| `pad_bottom` | 2.6 | Reach below the chin; the default lands roughly mid-chest. |
| `pad_sides` | 1.4 | Width either side of the face, as a fraction of face width. |
| `aspect_ratio` | source | Crop shape: `source`, `square`, `9:16`, `16:9`, `4:5`, `3:4`. |
| `output_long_edge` | 0 | Resize the crop's long edge to this; 0 keeps native pixels (no resampling). |
| `subject_index` | 0 | Which confidence-ranked face to frame in a group shot. |
| `confidence_threshold` | 0.6 | Minimum detector confidence for a face to count. |
| `output_directory` | "" | Optional folder to also save the zoomed image into. |

Outputs: `zoomed_image` (feed this to the lipsync or enhance step), `preview_image` with both the crop box and the detected face box drawn (check it while dialling in pads), `zoom_factor`, and `crop_region` — a `hyperreal.zoom_crop/1` JSON dict recording the source dimensions, the crop box, the detected face box, and the measured zoom factor. That dict is the still-image analogue of the head-region contract, and it is what Composite Face Back consumes.

## 6. Composite Face Back

Composite Face Back is the still-image twin of Composite Region Back: it pastes an enhanced face crop back onto the full-body source, consuming the `crop_region` from the Zoom To Head that made the original crop. It refuses to run if the base image's dimensions differ from the source dimensions the region records — that region belongs to a different image. The enhanced crop can come back at any resolution; it is Lanczos-resized to its computed placement, blended through a matte that is inset by `matte_inset_px`, shaped by `edge_shape`, and Gaussian-feathered by `feather_px`, with a per-channel mean/contrast color match on by default (two separate generations rarely agree on exposure).

The node's defining feature is auto-alignment, because generative editors rarely preserve composition — the re-rendered head usually occupies a different fraction of the canvas than the original did, and a naive box-to-box paste would land the face at the wrong size. Run a second Zoom To Head on the enhanced image and wire its `crop_region` into `enhanced_face_region`: the paste then derives scale from the ratio of the two face-box diagonals and position from the two face centers, and reports the measured drift and correction in `result_details`. Without that input (or with an older region that lacks a `face` entry), the node falls back to the naive box mapping and says so in a note. When the aligned insert lands lower than the original crop-box top — the generator zoomed in — `extend_top_coverage` extends it upward with backdrop replicated from the insert's own top edge so the original hair crown is fully replaced rather than surviving as a ghost fringe; it assumes clean backdrop above the head, so disable it when the top of the enhanced image is not background. `scale_adjust`, `offset_x`, and `offset_y` are manual nudges applied on top of whichever placement was computed.

> **Note:** Detect the enhanced face on a downscaled copy (around 640 px wide) rather than a full-resolution render — the detector is unreliable on very large faces. The node compensates for the size difference automatically: it rescales the detection's coordinates by the ratio between `face_image` and the source size the enhanced region records.

| Parameter | Default | Purpose |
|---|---|---|
| `base_image` | — | The original full-body image the crop was taken from. |
| `face_image` | — | The enhanced face crop to paste back (any resolution). |
| `crop_region` | — | The `crop_region` from the original Zoom To Head. |
| `enhanced_face_region` | — | Optional but strongly recommended: `crop_region` from a second Zoom To Head run on the enhanced image; enables auto scale/position correction. |
| `edge_shape` | rounded_rect | Matte shape: `rounded_rect` keeps most of the crop, `ellipse` hides seams best, `rectangle` keeps everything up to the feather. |
| `feather_px` | 24 | Gaussian feather on the paste edge. |
| `matte_inset_px` | 8 | Pull the matte edge inward before feathering. |
| `extend_top_coverage` | true | Replicate backdrop upward when the aligned insert starts below the original crop-box top. |
| `scale_adjust` | 1.0 | Manual scale nudge on top of the automatic result. |
| `offset_x` / `offset_y` | 0 | Manual position nudges in base-image pixels. |
| `color_match` | true | Per-channel mean/contrast transfer onto the base region. |
| `output_directory` | "" | Optional folder to also save the composite into. |

Outputs: `composited_image`, the full-body image with the enhanced face blended in, and `matte_preview`, a full-frame view of where the blend lands (white = enhanced content) — the thing to check when tuning scale, feather, and inset.
