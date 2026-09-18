---
title: Generation Service Nodes
section: Pipeline
subsection: Griptape Nodes
category: Generation Services
excerpt: Hosted HeyGen and WaveSpeed nodes for lipsync, translation, and image editing.
tags: [ai, video, pipeline, workflow, reference]
updated: 2026-09-18
---
> lede: Five HyperReal nodes drive hosted generation APIs — HeyGen for lipsync avatars and translation, WaveSpeed for talking-head video and image editing.

These nodes exist so that a shot can move through paid, hosted generation steps without anyone leaving the Griptape canvas or babysitting a vendor dashboard. Each one follows the same lifecycle: it resolves whatever media you wired in, submits a job to the vendor's API, polls until the job finishes or a deadline passes, then downloads the result and re-hosts it through the engine's static file store. That last step is not optional politeness — HeyGen returns presigned URLs that expire, and WaveSpeed deletes generated files after seven days, so a workflow that passed the vendor's URL downstream would rot silently. Every node also reports through the standard success/failure control outputs, with a human-readable summary in the status details rather than a stack trace.

## 1. Secrets and Shared Behavior

The HeyGen nodes require `HEYGEN_API_KEY`; the WaveSpeed nodes require `WAVESPEED_API_KEY`. Both are set under Settings, then API Keys and Secrets, and both are checked before the node runs so a missing key fails immediately instead of mid-workflow. Newly registered secrets only load at engine startup, so after adding a key for the first time you must restart the engine — refreshing libraries is not enough, and the WaveSpeed nodes say so explicitly in their error message.

Media inputs are deliberately forgiving. A parameter typed as an image or audio artifact accepts raw bytes, `http(s)` URLs, `data:` URIs, workspace-relative paths, absolute paths, and `{project_dir}/...` macro paths, and every node sniffs the actual content type from magic bytes because artifact metadata is unreliable across sources. The two vendors differ in how inputs reach them: HeyGen cannot fetch arbitrary URLs for asset-based jobs, so the nodes download the media and upload it to HeyGen's asset store, which caps files at 32 MB. WaveSpeed accepts URLs directly, so public `http(s)` URLs pass through untouched, while local files, localhost-hosted static URLs, and raw bytes are transparently uploaded through WaveSpeed's media API, which caps files at 200 MB.

Polling backs off exponentially (interval multiplied by 1.5 up to a ceiling) until a per-node deadline, after which the node raises a timeout that names the job id — the vendor job may still complete, and the id output lets you chase it. The HeyGen nodes retry HTTP 429 responses up to three additional times, honoring the `Retry-After` header capped at 60 seconds; the WaveSpeed nodes have no 429 retry, so a rate-limited WaveSpeed call fails outright. Every node takes an optional `output_directory` (macro paths supported) that also writes a file copy with `_1`, `_2` collision suffixes; a failed copy is reported in the status details but never fails the run.

## 2. HeyGen Avatar Video

Image plus audio in, lipsync video out, via HeyGen's Avatar IV engine — the only HeyGen engine that accepts arbitrary image input, which is why no engine parameter is exposed (omitting the field selects Avatar IV server-side). This is the node that turns an approved still and an approved voice track into the base performance clip for a shot. Both inputs are uploaded to HeyGen's `/v3/assets` endpoint, so each must fit the 32 MB cap; the job is submitted with a per-request idempotency key and polled for up to 30 minutes, starting at 5-second intervals and backing off to 15.

Two parameters deserve attention. `resolution` means the short edge: `1080p` returns 1080x1920 for portrait input and 1920x1080 for landscape, whatever size you fed in. And `aspect_ratio` should be set explicitly — `auto` is documented by HeyGen as following the input image, but was measured returning 16:9 for a portrait input, which pillarboxes the subject with black bars baked into the frame and poisons any downstream composite. The default is therefore `9:16`, and the parameter is connectable so a Shot Settings node can drive every clip in a shot. Keep `expressiveness` at `low` when two passes will be composited: camera drift differs between generations and makes the overlay slide.

| Parameter | Default | Purpose |
|---|---|---|
| image | — | The approved image to animate (png or jpeg, up to 32 MB) |
| audio | — | The approved audio to lipsync to (mp3 or wav, up to 32 MB) |
| video_title | (empty) | Optional title shown in the HeyGen dashboard; also names the saved file copy |
| aspect_ratio | 9:16 | Output aspect ratio; avoid `auto` (see above) |
| resolution | 1080p | Short-edge resolution, `1080p` or `720p` |
| motion_prompt | (empty) | Optional natural-language direction for body motion and gestures |
| expressiveness | low | Energy and movement range; keep low for composited passes |
| output_directory | (empty) | Optional folder to also save the video into |

Outputs: `video` (the generated clip, re-hosted locally) and `video_id` (the HeyGen id, usable for translation and debugging). On failure the node surfaces HeyGen's `failure_code` and `failure_message` verbatim in the status details.

## 3. HeyGen Video Translate

Takes a finished video — typically the Avatar Video output wired straight in — and produces translated versions with lip-sync in one or more target languages. All languages are submitted in a single API call, which returns one translation id per language; each id is then polled independently, so one failing language never discards the others. A run with partial failures still counts as a success so the surviving languages flow downstream, with each failure listed per-language in the status details. Only when every language fails does the node fail.

Requested language names are validated against HeyGen's live language list at submit time, with did-you-mean suggestions for near-misses — far better than an opaque error twenty minutes into a poll. If the language list cannot be fetched, validation is skipped with a logged warning rather than blocking the run. Source video handling follows the upload rules above: a public URL is passed to HeyGen as-is, while local files and localhost URLs are re-uploaded and therefore hit the 32 MB asset cap — the error for an oversized local video tells you to host it on a public URL instead. Polling runs up to 45 minutes across all languages, 5 seconds backing off to 15.

| Parameter | Default | Purpose |
|---|---|---|
| video | — | The source video to translate |
| target_languages | (empty list) | Target languages in HeyGen format, e.g. "Spanish (Spain)" |
| title | (empty) | Optional title prefix for the dashboard and saved-file names |
| mode | speed | `speed` for fast turnaround, `precision` for higher lip-sync quality |
| output_directory | (empty) | Optional folder to save all translated videos into |

Outputs: `videos` (a list of translated clips in the order of the successful languages) and `language_map` (JSON mapping language to translated video URL, for downstream routing).

## 4. WaveSpeed InfiniteTalk

Image plus audio in, talking video out, via WaveSpeed's InfiniteTalk model — the alternative talking-head generator to HeyGen, useful when a shot needs a different engine's look or WaveSpeed's pricing. Public URLs pass straight to the API; everything else uploads through WaveSpeed's media endpoint (200 MB cap). The prediction is polled for up to 30 minutes, 5 seconds backing off to 10, and the finished video is downloaded and re-hosted because WaveSpeed deletes files after seven days.

Resolution is the cost lever: per the node's own tooltip, 480p costs $0.03 per second and 720p $0.06 per second, with a $0.15 minimum per job. An optional mask image restricts which regions of the frame the model may animate, and a seed of `-1` (the default) picks a random seed while any other value makes the result reproducible.

| Parameter | Default | Purpose |
|---|---|---|
| image | — | The photo to animate |
| audio | — | The audio to lipsync to |
| prompt | (empty) | Optional guidance for expression, style, or pose |
| mask_image | (none) | Optional mask specifying the animatable regions |
| resolution | 480p | `480p` ($0.03/s) or `720p` ($0.06/s, $0.15 minimum) |
| seed | -1 | Seed for reproducible results; -1 picks a random seed |
| output_directory | (empty) | Optional folder to also save the video into |

Outputs: `video` (the generated clip) and `prediction_id` (the WaveSpeed id, for support and debugging).

## 5. WaveSpeed InfiniteTalk V2V

The video-to-video sibling: it relipsyncs an existing video to a new audio track instead of animating a still. Everything else — upload rules, resolution pricing, seed, mask, polling, retention — is identical to InfiniteTalk above. Use it when the performance already exists as video (a previous generation, a translated clip) and only the mouth needs to follow new audio.

> **Note:** The model path this node calls is `wavespeed-ai/infinitetalk/video-to-video`. The source carries a warning that one WaveSpeed documentation page spells the model "infinietalk"; if the first live run returns a 404, that path is the thing to re-check.

| Parameter | Default | Purpose |
|---|---|---|
| video | — | The base video to relipsync |
| audio | — | The audio to lipsync to |
| prompt | (empty) | Optional guidance for expression, style, or pose |
| mask_image | (none) | Optional mask specifying the animatable regions |
| resolution | 480p | `480p` ($0.03/s) or `720p` ($0.06/s, $0.15 minimum) |
| seed | -1 | Seed for reproducible results; -1 picks a random seed |
| output_directory | (empty) | Optional folder to also save the video into |

Outputs: `output_video` (the relipsynced clip) and `prediction_id`.

## 6. WaveSpeed Image Edit

Prompt plus reference images in, edited image out. One node covers three edit models — `google/nano-banana-pro/edit` (the default), `google/nano-banana-2/edit`, and `openai/gpt-image-2/edit` — because they share the same input signature; the model is a dropdown. The Google models accept at most 14 reference images and GPT Image 2 at most 16, and the node counts folder plus connected references against the limit before submitting, naming both counts in the error if you are over. A prompt is required, and at least one reference must come from somewhere. Polling is the fastest of the family: up to 10 minutes, 2 seconds backing off to 5.

Reference order matters, and the node makes it controllable. `reference_directory` points at a folder whose images are all used, sorted by filename, before anything connected to `images` — later references bias the generation more strongly, so a `01_`, `02_` naming convention gives deliberate control over weighting, and an anchor image wired to `images` always weighs most. The status details list the references in the order they were sent, so a surprising result can be audited. The folder accepts `.png`, `.jpg`, `.jpeg`, and `.webp`, and an empty or invalid folder fails with a readable error rather than silently sending fewer references.

| Parameter | Default | Purpose |
|---|---|---|
| prompt | (empty, required) | Text instruction describing the desired edit |
| reference_directory | (empty) | Optional folder of reference images, sorted by filename, used first |
| images | (empty list) | Connected reference images, used after the folder and weighted strongest |
| model | google/nano-banana-pro/edit | Which WaveSpeed edit model to use |
| aspect_ratio | auto | Output aspect ratio; `auto` omits the field so the model decides |
| resolution | 1k | Output tier: `1k`, `2k`, or `4k`; higher tiers cost more |
| quality | medium | Quality tier — only sent to `openai/gpt-image-2/edit`, ignored otherwise |
| output_format | png | `png` or `jpeg` |
| output_directory | (empty) | Optional folder to also save the edited image into |

Outputs: `image` (the edited result, re-hosted locally with its extension sniffed from the actual bytes) and `prediction_id`.
