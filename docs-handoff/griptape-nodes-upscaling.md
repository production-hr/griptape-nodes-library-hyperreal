---
title: Video and Image Enhancement
section: Pipeline
subsection: Griptape Nodes
category: Upscaling
excerpt: Topaz upscaling and local DLSS 5 detail recovery nodes for the pipeline.
tags: [ai, video, pipeline, workflow, reference]
updated: 2026-09-18
---
> lede: Three nodes recover resolution and detail — hosted Topaz upscaling for video and stills, and a locally-run DLSS 5 neural detail pass.

Generated video comes back at delivery-unfriendly resolutions, and head crops pulled out of full-body frames carry too few pixels for a lipsync model to animate cleanly; the two Topaz nodes fix both problems with Topaz Labs' hosted upscalers, and a third node runs NVIDIA DLSS 5 neural rendering on the local GPU to reconstruct the micro-detail — skin, hair, fabric — that upscaling alone cannot invent. The Topaz pair talks to two entirely separate API surfaces behind the same `TOPAZ_API_KEY` — the video node uses the express video flow, the image node the async image enhance flow — but shares the library's standard plumbing: media inputs in any of the usual forms (bytes, URLs, data URIs, workspace paths, `{project_dir}` macros), HTTP 429 retried up to three additional times honoring `Retry-After` capped at 60 seconds, results downloaded immediately and re-hosted through the engine's static file store because Topaz's download links expire, and an optional `output_directory` file copy with collision suffixes. Both Topaz nodes report through the standard success/failure control outputs, so a missing key, an oversize input, or a failed Topaz job surfaces as a readable status message rather than a stack trace. The key is set under Settings, then API Keys and Secrets, and a newly added key requires an engine restart before the nodes can see it; the DLSS 5 node, running locally, needs no secret at all.

## 1. Topaz Video Upscale

Upscales and optionally retimes a video through the Topaz Video API's express flow: the node creates a request describing the output, PUTs the source bytes to the upload URL Topaz returns, polls status for up to an hour (10-second intervals backing off to 30), then downloads the result — the download URL carries a 24-hour TTL, which is why the node re-hosts the file instead of passing Topaz's link downstream. The source may be mp4, mov, or mkv (the container is sniffed from magic bytes) up to Topaz's 500 MB request cap, which the node checks before uploading anything. Enhancement filters are sent with the model id alone, leaving Topaz's auto-parameter mode to tune everything else, and the output is always an mp4 with AAC audio copied through untouched — important when the clip is a lipsync whose sound must survive the round trip.

The model choice is the main decision: `prob-4` (Proteus) is the general-purpose default, `iris-3` (Iris) is face recovery and the right pick for talking-head avatars, `ahq-12` is Artemis HQ, and `nyx-3` (Nyx) is denoise. Frame interpolation is a separate, optional filter that runs in the same Topaz job: `none` (the default) plainly resamples to `output_frame_rate`, which is fine for downward conversions like 60 to 24; `chr-2` and `chf-3` (Chronos and Chronos Fast) suit rate conversions such as 24, 25, and 30 between each other; `apo-8` and `apf-2` (Apollo and Apollo Fast) suit big multipliers like a jump to 60 or slow motion, with the Fast variants trading quality for speed. Match `output_frame_rate` to the source when you are not interpolating — HeyGen outputs 25 fps, hence the default of 25.

| Parameter | Default | Purpose |
|---|---|---|
| video | — | The video to upscale (mp4/mov/mkv, up to 500 MB) |
| model | prob-4 | `prob-4` Proteus (general), `iris-3` Iris (face recovery), `ahq-12` Artemis HQ, `nyx-3` Nyx (denoise) |
| output_width | 3840 | Output width in pixels; use e.g. 2160 for portrait 9:16 |
| output_height | 2160 | Output height in pixels; use e.g. 3840 for portrait 9:16 |
| output_frame_rate | 25 | Output frame rate; match the source to avoid resampling |
| frame_interpolation | none | AI interpolation to reach the target rate: `chr-2`/`chf-3` Chronos, `apo-8`/`apf-2` Apollo |
| video_encoder | H264 | `H264` plays everywhere; `H265` gives smaller 4K files |
| output_directory | (empty) | Optional folder to also save the upscaled video into, named after the source with an `_upscaled` suffix |

Outputs: `upscaled_video` (the re-hosted result) and `request_id` (the Topaz id, for support and for chasing a job that outlived the one-hour poll — the timeout message says the job may still complete). On success the status details report the output size, elapsed time, and the credit estimate Topaz returned for the job.

## 2. Topaz Image Upscale

Restores and enlarges a still image through the Topaz Image API — a genuinely different surface from the video node: a multipart POST to the async enhance endpoint, a status poll (up to 15 minutes, 2-second intervals backing off to 10), then a download whose presigned link expires one hour after the job completes, so the node fetches the bytes immediately. The node was built for the zoomed-face lipsync pass: a head crop taken out of a full-body frame has too few pixels across the eyes and mouth, and the lipsync model then produces eyelid artifacting on blinks; enlarging the crop with real recovered detail before generation gives the model something to work from. For exactly that reason the node offers only the reconstruction models — High Fidelity V2, Standard V2, Low Resolution V2, CGI, and Text Refine — and deliberately excludes Topaz's generative family (Standard MAX, Recovery V2, Wonder, Redefine), which invents detail freely and is wrong for a likeness.

Three limits are enforced before any upload: 500 MB per request, 512 megapixels of input, and 1024 megapixels of computed output — the node decodes the image locally to know its dimensions, and also logs a warning if the requested `output_long_edge` would actually downscale. Face enhancement is on by default at strength 0.8, with creativity at 0; keep creativity at 0 for a real, recognisable person, because anything above lets Topaz invent facial detail and drift the likeness feeding the lipsync. The four fine-tuning floats — `denoise`, `sharpen`, `fix_compression`, `strength` — default to `-1`, which omits the field entirely so Topaz auto-tunes; that is not the same as `0`, which explicitly requests none.

> **Warning:** Sending both an output width and height letterboxes the result whenever the requested shape differs from the source's, and letterboxed frames silently corrupt a downstream lipsync pass. The node therefore sends only one dimension — the long edge — so Topaz scales the other proportionally, and it still compares the input and output aspect ratios afterwards, appending a warning to the status details if they diverged by more than one percent.

| Parameter | Default | Purpose |
|---|---|---|
| image | — | The image to upscale, e.g. the zoomed head crop |
| model | High Fidelity V2 | For clean, already-sharp sources; `Low Resolution V2` for soft originals, `Standard V2` as fallback, `CGI` for rendered art |
| output_long_edge | 1920 | Target longest side in pixels; only this dimension is sent so aspect is preserved; 0 lets the model pick its scale |
| face_enhancement | true | Topaz face recovery — the setting that matters for a lipsync source |
| face_enhancement_strength | 0.8 | How hard face recovery is applied, 0-1 |
| face_enhancement_creativity | 0.0 | 0 reconstructs only what is there; keep at 0 for a real person |
| subject_detection | All | Which part of the frame the model works on: `All`, `Foreground`, `Background`; older lowercase values are normalised |
| denoise / sharpen / fix_compression / strength | -1 | 0-1 knobs; -1 omits the field so Topaz auto-tunes |
| output_format | png | `png`, `jpeg`, `jpg`, `tiff`, or `tif`; png is lossless and right when the result feeds another generative pass — webp is not accepted on output |
| output_directory | (empty) | Optional folder to also save the result into |

Outputs: `upscaled_image` (the re-hosted result — feed this to the lipsync node), `process_id` (for chasing a job that timed out on our side), and `size_report` (JSON with source and output dimensions, the achieved scale factor, and the model and face-enhancement settings used). The status details additionally report the megapixel count the job was billed on and the aspect-ratio check described above.

## 3. DLSS 5 Enhance Video

Runs NVIDIA DLSS 5 neural rendering over a finished video to reconstruct the micro-detail AI generation is worst at — skin and hair texture rather than sharpening. Unlike everything else on this page, and unlike the library's other ComfyUI nodes, it executes on the local machine: the DLSS runtime drives the GPU directly through a native worker, so there is no hosted equivalent to call. The node wraps `DLSS5 Enhance Video File` from the `Blueforcer/ComfyUI-DLSS5-Enhancer` node pack and talks to a ComfyUI on `127.0.0.1`, probing port 8000 first (so an already-open ComfyUI Desktop is reused) and then 8188; an explicit `server_url` overrides the probe. Before submitting anything it checks that the ComfyUI actually has the node pack installed, failing with a message that names the fix (clone into `custom_nodes`, run `install_runtime.py`, restart ComfyUI). No API secret is involved, and the auto-start path detection targets Windows ComfyUI Desktop installs.

> **Warning:** With `auto_start_server` on (the default), the node launches a detached headless ComfyUI on port 8188 when neither port answers, waiting up to 180 seconds for it to become ready (its log lands in the system temp directory). That process deliberately outlives the node and the workflow — later runs reuse it instead of paying the startup cost again — but nothing ever shuts it down for you. Close it yourself when you are done, or turn `auto_start_server` off and manage ComfyUI manually.

The defaults are the ones that measured best on HyperReal footage, not the node pack's own. `local_tone_strength` is 0.0 rather than the pack's 1.0: at 1.0 the enhancer applies a local tone map that crushes highlights and desaturates by roughly 18 percent — that, not the neural pass, is the entire colour shift — while at 0.0 it is colour neutral. `dlss_model_preset` is M, which retains the most texture, and `upscaling_mode` stays at 1x because the other modes do nothing on the community RTX 40-series runtime, which falls back to native. Two things decide whether the pass helps at all, and neither is a setting. Motion: static and slow shots gain detail, fast-motion shots lose it — set `motion` to `none` there to limit the damage, or skip the pass entirely. Resolution: at HD the result is roughly parity, while at 4K it returns about 2.1x the detail for about 1.46x the shimmer, so upscale before this node, not after.

Mechanically, a local source file is handed to ComfyUI by path and never passes through memory; URLs are streamed to a temp file first. The node polls ComfyUI's history every two seconds up to `timeout_seconds` (default 3600), and cancelling the workflow in the editor is handled cleanly — an interrupt is sent to ComfyUI and the node reports `cancelled`. `max_frames` renders a preview of that many frames instead of the whole clip, which is the cheap way to judge a shot before committing.

| Parameter | Default | Purpose |
|---|---|---|
| video | — | Source video: artifact, URL, or a plain local path; upscale before this node |
| dlss_model_preset | M | DLSS model: `Default`, `J`, `K`, `L`, `M`; M retains the most skin and hair texture |
| local_tone_strength | 0.0 | Local tone mapping; keep at 0.0 for colour neutrality (see above) |
| motion | auto | Temporal accumulation: `auto`, `optical_flow`, or `none`; use `none` on fast motion |
| upscaling_mode | 1x (DLAA / native) | Leave at 1x on RTX 40-series; the community runtime rejects the other modes |
| output_directory | (empty) | Absolute path to write into; empty writes to ComfyUI's output folder |
| codec | H.264 | `H.264`, `HEVC`, `AV1`, or `ProRes Proxy`; H.264 and HEVC use NVENC with a software fallback |
| container | MP4 | `MKV` stream-copies audio and subtitles; `MP4` and `MOV` re-encode audio to AAC |
| quality | Good | `Max` is constant quality and very large (~8 MB per second); use it when measuring |
| copy_audio | true | Mux the original audio into the result |
| local_structure_strength | 1.5 | Detail reconstruction; measured effect between 1.5 and 2.0 is negligible |
| skin_structure_strength | 2.0 | Skin and pore reconstruction; requires `automatic_mask` |
| automatic_mask | true | Let the model detect skin regions; the gate for skin structure |
| nr_style | Default | Look of the neural pass: `Default`, `Natural`, `Cinematic` |
| nr_preset | Default | Measured to produce identical output at every value on current builds |
| nr_intensity | 1.0 | Strength of the neural pass; above 1.0 has no further effect |
| scene_change_threshold | 0.24 | Measured to make no difference between 0.24 and 0.90 |
| max_frames | 0 | 0 renders the whole video; any other value renders a preview of that many frames |
| server_url | (empty) | ComfyUI base URL; empty probes 127.0.0.1:8000 then :8188 |
| auto_start_server | true | Launch a headless ComfyUI if none is reachable (see the warning above) |
| comfy_python / comfy_main | (empty) | Auto-start executables; empty auto-detects the ComfyUI Desktop install |
| timeout_seconds | 3600 | Maximum seconds to wait for the render |

Outputs: `video_out` (the enhanced video, re-hosted through the static file store), `output_path` (the absolute path of the file DLSS 5 wrote, for a local next step), `frames` (frames processed), `status` (`completed`, or a `kind: detail` string on failure), and `was_successful`. This node does not use the standard success/failure control outputs the rest of the page's nodes share — it is a plain control node, and failures surface through `status` and `was_successful` instead.
