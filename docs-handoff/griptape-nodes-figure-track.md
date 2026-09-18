---
title: Figure Track Nodes
section: Pipeline
subsection: Griptape Nodes
category: Figure Prep
excerpt: Track a performer, crop for SCAIL, and reposition the generated character.
tags: [video, pipeline, character, comfyui, ai, workflow]
updated: 2026-09-18
---
> lede: Three nodes — Detect Figure Track, Reposition Tracked Crop, and RunComfy SCAIL Infinite — carry a full-body character replacement from greenscreen plate to 4K comp source.

A dancer moving across a landscape stage forces a wide framing, and a wide framing starves the generated character of pixels — the face worst of all. The Figure Prep answer is to stop generating from the wide plate entirely: track the performer on the greenscreen, cut a tight full-height crop that pans with them, run only that crop through SCAIL (character-swap video generation on a RunComfy serverless ComfyUI deployment), then place the result back into wide-frame coordinates using the recorded track. On the first production plate this bought roughly three times the resolution on the figure compared to generating wide, and because the reposition step can render its canvas at a multiple of the plate size, every pixel the generator made survives to 4K with zero resampling. Detect Figure Track and Reposition Tracked Crop are the bookends of that loop; RunComfy SCAIL Infinite is the generator call in the middle. The two Figure Prep nodes run locally on OpenCV and ffmpeg with no API or secrets; the SCAIL node needs the `RUNCOMFY_TOKEN` secret. All three nodes report success or failure through the library's standard status outputs (a success boolean plus a human-readable details string), which are not repeated in the parameter tables below.

> **Note:** Detect Figure Track emits the same `hyperreal.head_region/1` region dict as Detect Head Region, so the stock Crop To Region node cuts the actual panning crop, and Reposition Tracked Crop consumes the same dict on the way back. The typical wiring is Load Video → Detect Figure Track → Crop To Region → SCAIL → Reposition Tracked Crop.

## 1. Detect Figure Track

Detect Figure Track finds the performer without any ML: on a chroma plate, the figure is simply whatever is not the backdrop, in front of the backdrop. Each frame is reduced to per-column greenscreen coverage — a pixel counts as backdrop when its key channel (green or blue) exceeds the other two channels by `key_threshold` — and the screen itself bounds the search on all four sides. Columns containing at least 10 percent backdrop define the stage's horizontal extent, robust percentiles of the screen's first and last green rows define a vertical band (excluding the wall above a sagging cloth top and the floor in front), and a strict pass keeps only columns that are at least half green inside that band, which drops the shadowed drape edges at the screen's left and right. What remains is a per-column subject profile with the median column baseline subtracted, so residual spill and a non-green floor strip do not inflate the box. Because limbs attach to the body, the figure must be one contiguous run of columns: only the run with the greatest total mass is kept (gaps up to eight columns are bridged for motion-blurred wrists), so disconnected blobs — sandbags, shadowed cloth corners, a light-stand pole in front of the screen — are discarded wherever they sit.

The output window is deliberately simple: full plate height and one clip-wide width — the widest silhouette plus `margin_px` on each side, snapped up to a multiple of `snap_multiple` — panning horizontally on a smoothed track. Think lazy camera operator, not lock-on. The desired path is a moving average of the silhouette centre, but containment is a hard constraint: per-frame intervals guarantee the silhouette never exits the window. Those constraint tracks are median-3 filtered, so a single-frame silhouette-edge spike from motion blur cannot yank the window for one frame and release it — real moves last two or more frames and survive the filter, and the margin absorbs the one-frame excursions the filter smooths over. Pan speed is capped at `max_pan_speed` pixels per frame (0 means auto, the larger of 8 px and 1.5 percent of plate width), and the feasible intervals are propagated backward in time first, so the track ramps in ahead of a sudden kick or lunge instead of jerking when it lands. The limit is only exceeded when the figure genuinely outruns it. In `auto` box mode the node measures the track's drift and collapses to a single static window when the swept union area is within 15 percent of one window's area.

Frames where no figure is detectable are interpolated across, and the result details say how many were missed. The details also warn when the crop width had to be clamped to the plate width and when the silhouette touches the plate's own left or right edge (meaning the figure is cut off in the source footage itself, and no crop can fix that). If no frame yields a figure at all, the node fails with a pointer at `key_color` and `key_threshold`.

> **Warning:** `margin_px` is absolute pixels, not a fraction of the plate. The default 48 was proven on 1920x1080 plates; a UHD-scale plate needs roughly double that to buy the same relative breathing room around an extended arm.

> **Warning:** Always scrub the `preview_video` output before spending SCAIL credits — the magenta window drawn on the plate should follow the performer smoothly and never clip a limb. The preview is free; a SCAIL run is not. `key_threshold` is plate-dependent: 40 suits a well-lit saturated screen, while one flat, low-saturation production plate needed 20 (at 40 the node found no screen at all).

| Parameter | Default | Purpose |
|---|---|---|
| `video` | — | The chroma-key plate to analyze (figure against green or blue). |
| `key_color` | `green` | Backdrop colour; the subject is every pixel that is not this colour. |
| `key_threshold` | 40 | How dominant the key channel must be (0-255) to count as backdrop; lower for flat plates, raise if screen shadows read as subject. |
| `margin_px` | 48 | Horizontal breathing room added to each side of the widest silhouette. |
| `smoothing_window` | 25 | Moving-average window in frames on the pan track; bigger means a lazier camera. |
| `max_pan_speed` | 0 | Pan speed limit in px/frame; 0 = auto (about 1.5 percent of plate width). |
| `box_mode` | `auto` | `static` for one fixed window, `tracked` for a per-frame pan, `auto` decides from measured drift. |
| `snap_multiple` | 16 | Crop width is snapped up to a multiple of this for codec-friendly dimensions. |
| `column_threshold` | 0.04 | Fraction of the strongest column a column must reach to count as subject; the default keeps thin extended arms, raise it if spill blobs get grabbed. |

Outputs: `region` (the full track dict), flat `x`, `y`, `width`, `height` integers that wire straight into the stock Crop Video node, and `preview_video` for QC.

## 2. Reposition Tracked Crop

Reposition Tracked Crop is the inverse of Crop To Region for pipelines that do not paste back onto the plate. SCAIL's replacement mode repaints the whole crop — backdrop included — and finishing happens in Resolve or Nuke anyway, so the plate's only remaining contribution is coordinates, and the track already carries those. Feed the node the generated character clip (or its matte — black padding is exactly what a matte wants) plus the region dict, and it renders a full-frame video with the crop placed at each frame's recorded offset over a flat background. It refuses anything that is not a `hyperreal.head_region/1` dict, and the input is scaled to the recorded crop size before placement (area interpolation shrinking, Lanczos enlarging), so a generator returning unexpected dimensions never breaks alignment.

`output_scale` is where the 4K happens. The canvas is rendered at this multiple of the plate size, and the crop's placement box and per-frame offsets scale with it. Match it to the generator's upsample of the crop and the clip lands 1:1 with no resampling at all: a 640x1080 crop generated at 1280x2160 and repositioned at `output_scale` 2.0 produces a 3840x2160 clip that keeps every generated pixel — no downscale-then-upscale round trip, nothing hallucinated. The result details confirm which path was taken: look for "placed 1:1" rather than "resampled". Run the matte through the same node with the same region and scale and both land in the identical comp. The node warns when the input's frame count differs from the track's — benign on deliberately trimmed runs, a red flag otherwise — and the output carries no audio, because it is a comp source, not a deliverable.

| Parameter | Default | Purpose |
|---|---|---|
| `video` | — | Crop-space video: the generated character clip, or its matte. |
| `region` | — | Region dict from Detect Figure Track (or Detect Head Region). |
| `output_scale` | 1.0 | Canvas as a multiple of the plate size; match the generator's upsample for a 1:1, zero-resample placement. |
| `background` | `black` | Flat fill outside the tracked window (`black`, `gray`, `white`, `green`); black is correct for mattes and comp sources. |
| `crf` | 12 | x264 quality; 12 is near-lossless on purpose since this is a comp source. |
| `pix_fmt` | `yuv420p` | `yuv444p` preserves chroma into the comp but chokes some tools; try 420 first. |
| `output_directory` | (empty) | Optional folder (macros like `{project_dir}/outputs` work) to also save a file copy; empty skips it. |

Outputs: `repositioned_video` (the full-frame clip) and `region_out` (the region dict passed through unchanged, for chaining the matte pass).

## 3. RunComfy SCAIL Infinite

RunComfy SCAIL Infinite drives a serverless ComfyUI deployment on RunComfy: it submits an inference request with per-node input overrides, polls the async queue, and returns the generated video's URL. The API token comes from the `RUNCOMFY_TOKEN` secret (Griptape settings or an environment variable, checked in that order), and the node refuses to run without it or without a `deployment_id`. The main inputs map onto specific nodes inside the deployed workflow: the reference character image, a positive/subject text, the motion (driving) video — which in this pipeline is the panning crop from Crop To Region — and the main prompt. Blank inputs are simply not overridden, so the workflow's own values stand. `video_width` and `video_height` default to 1080x1920 and a value of 0 leaves the workflow's default in place; an optional LoRA is applied only when `lora_name` is set.

The node uploads nothing. `image_url` and `motion_video_url` are passed to RunComfy as strings, so they must be URLs the service can fetch — public HTTPS, or a data URI — and there is no file-size handling in the node itself; staging the crop somewhere reachable is the workflow's job. Likewise there is no dedicated frame-rate parameter: settings that live inside the deployed ComfyUI graph, such as the sampler's `force_rate` or frame budget, are reached through `extra_overrides_json`, a JSON object merged last into the overrides (for example `{"25": {"inputs": {"noise_seed": 42}}}`), which can therefore also override anything the named parameters set. The Advanced group holds the ComfyUI node ids the overrides target (image 311, positive text 568, motion video 614, prompt 647, LoRA 463, width 330, height 331 by default) — these only change when the deployment's graph changes.

Polling is cancel-aware and self-cleaning. The node checks status every `poll_interval` seconds until a terminal status arrives or `timeout_seconds` (default 6000, i.e. 100 minutes) elapses; on timeout, on ten consecutive poll failures, or when you press stop in the editor, it asks RunComfy to cancel the queued or running job rather than leaving it burning credits. On success it fetches the result payload, scans the outputs for URLs, prefers one with a video extension, and emits it as `video_url`; a finished job with no video URL in its outputs is reported as a failure.

> **Warning:** SCAIL does not return the driver's timeline verbatim. The first production run came back at 16 fps with the first two seconds of the driver silently dropped, and both plausible assumptions about the mapping (uniform squeeze; sampling from frame zero) produced visible horizontal sliding in the comp. Measure the time mapping against the driver before repositioning, or retime to the driver's rate first — and trim driver clips only from the start, because the track's offsets are matched by frame index.

| Parameter | Default | Purpose |
|---|---|---|
| `deployment_id` | (SCAIL deployment id) | RunComfy deployment to run; required. |
| `image_url` | (empty) | Reference character image URL (public HTTPS or data URI). |
| `positive_text` | (empty) | Positive/subject text override. |
| `motion_video_url` | (empty) | Driving video URL — the tracked crop in this pipeline. |
| `prompt_text` | (empty) | Main prompt text override. |
| `video_width` | 1080 | Output width in px; 0 leaves the workflow default. |
| `video_height` | 1920 | Output height in px; 0 leaves the workflow default. |
| `lora_name` | (empty) | Optional LoRA filename; blank means no LoRA override. |
| `lora_strength` | 0.85 | LoRA model strength, applied only when `lora_name` is set. |
| `extra_overrides_json` | (empty) | JSON merged last into the overrides; the route to any workflow setting without a dedicated parameter. |
| `poll_interval` | 5 | Seconds between status polls. |
| `timeout_seconds` | 6000 | Maximum wait before the node gives up and cancels the RunComfy job. |

Outputs: `video_url` (the generated clip as a URL artifact), `request_id` (for finding the run in RunComfy), `status` (final status string), and `result_json` (the full result payload, or the failure detail when a run fails).

## 4. Running the Pipeline

Two rules keep the round trip honest. First, aspect ratio is sacred: crop, SCAIL output, and reposition must all agree (640x1080 is 16:27, so 1280x2160 is the known-good 2x output), because any padding or letterboxing anywhere breaks the geometry the track encodes. Second, frame index is track index: interpolated gaps in detection are fine (the node reports them), but any temporal change to the clip — SCAIL's own sampling, a retime, a trim — must be accounted for before repositioning, or the character slides against his own steps. Save one workflow per take after its run: the saved file freezes that take's exact region, tuning, and crop dimensions, and is the source of truth when the SCAIL result comes back days later.
