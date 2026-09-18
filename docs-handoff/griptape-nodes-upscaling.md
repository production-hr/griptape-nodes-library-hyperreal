---
title: Topaz Upscaling Nodes
section: Pipeline
subsection: Griptape Nodes
category: Upscaling
excerpt: Topaz Labs video and image upscaling nodes, their models, limits, and gotchas.
tags: [ai, video, pipeline, workflow, reference]
updated: 2026-09-18
---
> lede: Two nodes wrap the Topaz Labs APIs — one upscales finished video, the other restores still images before a lipsync pass.

Generated video comes back at delivery-unfriendly resolutions, and head crops pulled out of full-body frames carry too few pixels for a lipsync model to animate cleanly; these two nodes fix both problems with Topaz Labs' hosted upscalers. They talk to two entirely separate API surfaces behind the same `TOPAZ_API_KEY` — the video node uses the express video flow, the image node the async image enhance flow — but share the library's standard plumbing: media inputs in any of the usual forms (bytes, URLs, data URIs, workspace paths, `{project_dir}` macros), HTTP 429 retried up to three additional times honoring `Retry-After` capped at 60 seconds, results downloaded immediately and re-hosted through the engine's static file store because Topaz's download links expire, and an optional `output_directory` file copy with collision suffixes. Both nodes report through the standard success/failure control outputs, so a missing key, an oversize input, or a failed Topaz job surfaces as a readable status message rather than a stack trace. The key is set under Settings, then API Keys and Secrets, and a newly added key requires an engine restart before the nodes can see it.

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
