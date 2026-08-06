# HyperReal Nodes for Griptape Nodes

HyperReal's custom node library for [Griptape Nodes](https://www.griptapenodes.com/) — one library, one install, with all our service integrations organized as categories:

```
HYPERREAL NODES
├─ VIDEO
│  ├─ HEYGEN            HeyGen Avatar Video · HeyGen Video Translate
│  ├─ TOPAZ             Topaz Video Upscale
│  ├─ WAVESPEED         WaveSpeed InfiniteTalk · WaveSpeed InfiniteTalk V2V
│  └─ FACE PREP         Detect Head Region · Crop To Region · Composite Region Back
├─ IMAGE
│  └─ WAVESPEED         WaveSpeed Image Edit
├─ STORAGE
│  └─ SPACES            Upload to Spaces
└─ VIEWCOMFY (planned)
```

Current contents: **HeyGen** nodes — generate lipsync avatar videos from an image + audio (Avatar IV), and translate videos into other languages with lip-sync and voice preservation; the **Topaz Video Upscale** node — upscale videos (e.g. HeyGen 1080p output → 4K deliverables) via the Topaz Labs API; and the **Upload to Spaces** node, which puts any media artifact into a DigitalOcean Spaces bucket and returns a public URL (needed by external APIs like ViewComfy that must fetch inputs over HTTP).

[![Add to Griptape Nodes](https://img.shields.io/badge/Add%20to-Griptape%20Nodes-blue)](https://nodes.griptape.ai/#library-management?git=https://github.com/production-hr/griptape-nodes-library-hyperreal)

**Install link:** <https://nodes.griptape.ai/#library-management?git=https://github.com/production-hr/griptape-nodes-library-hyperreal>

## Target pipeline

```
approved image ─┐
                ├─► HeyGen Avatar Video ─► English lipsync video ─► HeyGen Video Translate ─► [ES, CA, ...]
approved audio ─┘
```

Verified end to end in production on 2026-08-03.

## Setup

1. **Install the library** with the link above, or manually: Settings → Libraries → *+ Add Library* → path to `hyperreal/griptape_nodes_library.json` in your clone of this repo (absolute paths work; the repo does not need to live in your workspace directory).
2. **Set your secrets** under Settings → API Keys & Secrets, then **restart the engine** — see Gotchas:
   - `HEYGEN_API_KEY` — from the [HeyGen dashboard](https://app.heygen.com/settings?nav=API)
   - `TOPAZ_API_KEY` — from the [Topaz Labs API dashboard](https://www.topazlabs.com/api)
   - `WAVESPEED_API_KEY` — from the [WaveSpeed dashboard](https://wavespeed.ai/)
   - `DO_SPACES_KEY` / `DO_SPACES_SECRET` — a DigitalOcean Spaces access key pair (DO control panel → API → Spaces Keys)
   - `DO_SPACES_REGION` — e.g. `atl1` (or set `DO_SPACES_ENDPOINT`, e.g. `https://atl1.digitaloceanspaces.com`; either one suffices, the other is derived). **The endpoint must NOT include the bucket name** — see Gotchas.
3. Refresh Libraries. The nodes appear under **HyperReal Nodes → Video → HeyGen** and **→ Storage → Spaces**.

Requires engine version 0.86.0+ (the nodes use `SuccessFailureNode` and the project/macro path system).

## Nodes

Both nodes extend `SuccessFailureNode`: they have **Succeeded** / **Failed** control outputs plus `was_successful` (bool) and `result_details` (str) status outputs. API failures surface HeyGen's `failure_code` / `failure_message` as readable errors, never bare stack traces.

### HeyGen Avatar Video (`HeyGenAvatarVideo`)

Image + audio → lipsync video, using HeyGen's **Avatar IV** engine — the only engine that accepts arbitrary image input (Avatar V requires a registered digital twin, per HeyGen's model compatibility matrix). No `engine` field is sent; Avatar IV is the server default.

| Parameter | Type | Notes |
|---|---|---|
| `image` | ImageArtifact / ImageUrlArtifact | png or jpeg, up to 32 MB |
| `audio` | AudioArtifact / AudioUrlArtifact | mp3 or wav, up to 32 MB |
| `video_title` | str | Optional (the API does not require it); shown in the HeyGen dashboard |
| `aspect_ratio` | auto / 16:9 / 9:16 / 4:5 / 5:4 / 1:1 | `auto` follows the input image |
| `resolution` | 1080p / 720p | Default `1080p`. Unset, HeyGen defaults image jobs to 720p. 4k is Avatar III-only |
| `motion_prompt` | str | Optional natural-language gesture direction |
| `expressiveness` | low / medium / high | Default `low` |
| `output_directory` | str | Optional folder to also save the video into (supports `{project_dir}/...`) |

Outputs: `video` (VideoUrlArtifact, saved to static files), `video_id` (str).

Generation typically takes 2–5 minutes for short clips; the node polls up to 30 minutes (5s → 15s backoff).

### HeyGen Video Translate (`HeyGenVideoTranslate`)

Video → one or more translated videos with lip-sync and the original voice preserved.

| Parameter | Type | Notes |
|---|---|---|
| `video` | VideoUrlArtifact | Connect the Avatar Video output directly |
| `target_languages` | list of str | HeyGen language names — see identifier notes in Gotchas |
| `title` | str | Optional; also used as the saved-file name prefix |
| `mode` | speed / precision | `precision` gives higher lip-sync quality |
| `output_directory` | str | Optional folder to save all translated videos into (supports `{project_dir}/...`); files are named `<title>_<language>.mp4`, with `_N` suffixes instead of overwriting |

Outputs: `videos` (list of VideoUrlArtifact, in target-language order), `language_map` (language → URL).

All languages go in **one API call**; each returned translation ID is polled independently, so **a failing language never discards the other results** — failures are listed per-language in `result_details`. Requested languages are validated against HeyGen's live language list at submit time, with did-you-mean suggestions.

### Topaz Video Upscale (`TopazVideoUpscale`)

Video → upscaled video via the [Topaz Labs Video API](https://developer.topazlabs.com/) express flow: create request → PUT the file to the returned upload URL → poll → download (their download URL has a 24 h TTL, so the result is re-hosted via static files like the HeyGen outputs).

| Parameter | Type | Notes |
|---|---|---|
| `video` | VideoUrlArtifact | mp4/mov/mkv, up to 500 MB (Topaz request cap) |
| `model` | prob-4 / iris-3 / ahq-12 / nyx-3 | Proteus (general, default) · Iris (face recovery — best for talking-head avatars) · Artemis HQ · Nyx (denoise). Sent with Topaz's auto-parameter mode — no manual tuning |
| `output_width` / `output_height` | int | Default 3840×2160; swap for portrait (2160×3840 for the 9:16 welcome videos) |
| `output_frame_rate` | int | Default 25 — HeyGen outputs 25 fps; match the source to avoid resampling. With `frame_interpolation` set, this is the AI-interpolated target rate |
| `frame_interpolation` | none / chr-2 / chf-3 / apo-8 / apf-2 | Default `none` (plain resampling — fine for downward conversions like 60→24). Chronos (`chr-2`, `chf-3` fast) for rate conversions like 24↔25↔30; Apollo (`apo-8`, `apf-2` fast) for big jumps like →60 or slow-mo. Runs in the **same Topaz job** as the upscale — one upload, one encode, one billing pass |
| `video_encoder` | H264 / H265 | H264 (default) plays everywhere; H265 gives smaller 4K files |
| `output_directory` | str | Optional folder to also save into (supports `{project_dir}/...`); named `<source>_upscaled.mp4` with `_N` collision suffixes |

Outputs: `upscaled_video` (VideoUrlArtifact), `request_id` (str). Audio is copied through untouched (`audioTransfer: Copy`) — important for lipsync. `result_details` reports the billed credit estimate (Topaz bills the **lower bound** of the estimate; the first API request is free). Polls up to 60 minutes (10s → 30s backoff).

### WaveSpeed nodes (`WaveSpeedImageEdit`, `WaveSpeedInfiniteTalk`, `WaveSpeedInfiniteTalkV2V`)

Three nodes over the [WaveSpeed AI](https://wavespeed.ai/) platform, grouped by input signature. All share the same plumbing: media inputs in any of the usual forms; already-public http(s) URLs are passed to WaveSpeed as-is, everything else (local files, `localhost` static URLs, raw bytes) is transparently uploaded via WaveSpeed's media API (200 MB cap, 7-day retention); results are downloaded and re-hosted via static files (WaveSpeed deletes files after 7 days), with the usual optional `output_directory`.

**WaveSpeed Image Edit** — prompt + reference images → edited image, with a model dropdown:

| Parameter | Type | Notes |
|---|---|---|
| `prompt` | str | The edit instruction |
| `images` | list of Image artifacts | Up to 14 (Google models) / 16 (GPT Image 2), validated per model |
| `model` | dropdown | `google/nano-banana-pro/edit` (default) · `google/nano-banana-2/edit` · `openai/gpt-image-2/edit` |
| `aspect_ratio` | auto + 10 ratios | `auto` omits the field and lets the model decide |
| `resolution` | 1k / 2k / 4k | Prices range ~$0.02–$0.73 per image depending on model/quality/resolution |
| `quality` | low / medium / high | **GPT Image 2 only**; ignored by the Google models |
| `output_format` | png / jpeg | |

Outputs: `image` (ImageUrlArtifact), `prediction_id`.

**WaveSpeed InfiniteTalk** (image + audio → talking video) and **WaveSpeed InfiniteTalk V2V** (video + audio → relipsynced video) differ only in their media input:

| Parameter | Type | Notes |
|---|---|---|
| `image` / `video` | Image / Video artifact | The photo to animate (InfiniteTalk) or base video to relipsync (V2V) |
| `audio` | Audio artifact | The track to lipsync to |
| `prompt` | str | Optional expression/style/pose guidance |
| `mask_image` | Image artifact | Optional; restricts the animatable regions |
| `resolution` | 480p / 720p | $0.03/s vs $0.06/s, $0.15 minimum, 10-minute cap per job |
| `seed` | int | -1 = random |

Outputs: `video` (InfiniteTalk) / `output_video` (V2V), `prediction_id`.

### Face Prep nodes (`DetectHeadRegion`, `CropToRegion`, `CompositeRegionBack`)

Three local-processing nodes (OpenCV + ffmpeg — **no API, no secrets**) that bracket an external face-swap step. The point is resolution: swap against a 512–1024 px head crop instead of a head that occupies 15% of a 1080p frame.

```
Load Video → Detect Head Region → Crop To Region → Topaz Video Upscale → [external face swap] → Composite Region Back → Save Video
```

**The `region` contract** (`hyperreal.head_region/1`, param type `json`) is what makes crop/paste-back invertible — emitted by Detect, passed through Crop, consumed by Composite:

```json
{
  "schema": "hyperreal.head_region/1",
  "source":   { "width": 1920, "height": 1080, "frame_rate": 25.0, "frame_count": 250 },
  "box":      { "x": 704, "y": 96, "width": 512, "height": 512, "confidence": 0.94 },
  "mode":     "static",
  "offsets":  [[704, 96], [705, 96], "..."],
  "detector": "yunet-2023mar",
  "notes":    { "drift_px": 12, "detection_interval": 5, "frames_missed": 0, "clamped": false }
}
```

`box` uses the ecosystem's face-detection key names so stock nodes can consume it; `offsets` is per-frame top-left origins (length == `frame_count`); box **size never varies within a clip** — only position — so the crop is a pure pixel copy and paste-back needs no inverse scaling.

- **Detect Head Region** — YuNet face detector (vendored MIT-licensed ONNX at `hyperreal/faceprep/models/`, nothing downloads at run time) sampled every `detection_interval` frames; face box → head box via `pad_top`/`pad_bottom`/`pad_sides` (or `head_scale` when pads are all 0), squared, snapped up to `snap_multiple` (64). `box_mode` auto/static/tracked — auto picks static when measured drift is negligible. Subject followed by nearest centroid after the first confidence-ranked pick, so two-person shots don't swap subjects mid-clip. Outputs: `region`, flat `x`/`y`/`width`/`height` (wire into stock Crop Video if you prefer), and `preview_video` with the box drawn on — eyeball it to catch a bad detection. Missing faces in some sampled frames → interpolated with a warning; no faces at all → readable failure.
- **Crop To Region** — static mode is a single ffmpeg `crop` pass; tracked mode streams frames through a rawvideo pipe and slices with numpy at each frame's offset. CRF 12 (near-lossless swap intermediate), `yuv420p`/`yuv444p`. `audio_source`: `plate` (default — copies the plate's audio, falling back to a **silent track** when the plate has none, because face-swap tools often require an audio track to be present) / `silent` / `none`. Outputs `head_video` + `region_out` (pass-through).
- **Composite Region Back** — **validates before working**: insert frame count ≠ region's → fail naming both counts; fps or plate-dimension mismatch → fail. Lanczos-downscales the insert to box size, blends with an erode-then-feather procedural matte (`ellipse`/`rounded_rect`) or an optional `mask_video` matte (e.g. from a SAM video node), optional per-frame LAB `color_match`, remuxes the plate's audio. Output `composited_video`.

First run of any of these downloads the ffmpeg binaries once (via `static-ffmpeg`).

### Upload to Spaces (`UploadToSpaces`)

Media artifact → object in a DigitalOcean Spaces bucket, returning its public URL. Spaces is S3-compatible, so the node uses `boto3` with a custom `endpoint_url` — no DO-specific SDK.

| Parameter | Type | Notes |
|---|---|---|
| `artifact` | Image/Audio/Video artifact (URL or raw) | All the usual input forms work: bytes, `http(s)` URLs, `data:` URIs, workspace paths, `{project_dir}` macros |
| `bucket` | str | Spaces bucket name |
| `key_prefix` | str | e.g. `gaudi/welcome/`; a trailing `/` is added if missing |
| `filename` | str | Optional override; defaults to the artifact's filename, falling back to a generated name with a content-sniffed extension |
| `public` | bool | Default true (`ACL: public-read`); ViewComfy needs public URLs |

Outputs: `url` (`https://<bucket>.<region>.digitaloceanspaces.com/<key>`) and `key` (for downstream deletes/replacements). The upload's `ContentType` is sniffed from magic bytes so browsers and APIs treat the object correctly. Credential, bucket, and network failures each produce a distinct readable error naming the secret or setting to check.

## HeyGen API contract (as built)

All calls target `https://api.heygen.com` with an `X-Api-Key` header, per-submission `Idempotency-Key` (UUID), and `Retry-After`-aware retry on HTTP 429.

- **Upload asset** — `POST /v3/assets`, `multipart/form-data` with a `file` field (max 32 MB; png/jpeg/mp3/wav/mp4 and more). Response: `data.asset_id` (one unified ID for every media type — there is no separate `image_key`).
- **Create video** — `POST /v3/videos` with the `type: "image"` payload variant:

  ```json
  {
    "type": "image",
    "image": {"type": "asset_id", "asset_id": "..."},
    "audio_asset_id": "...",
    "title": "...",
    "aspect_ratio": "9:16",
    "resolution": "1080p",
    "motion_prompt": "...",
    "expressiveness": "low"
  }
  ```

  `audio_asset_id` / `audio_url` / `script`+`voice_id` are mutually exclusive; these nodes always use audio. Response: `data.video_id`.
- **Video status** — `GET /v3/videos/{video_id}`. `status`: `pending | processing | completed | failed`; on completion `video_url` (presigned), `thumbnail_url`, `duration`; on failure `failure_code` + `failure_message`.
- **Create translation** — `POST /v3/video-translations` with `video` (`{"type": "url"|"asset_id", ...}`), `output_languages` (**array** — one call for all languages), optional `title`, `mode`. Response: `data.video_translation_ids`, one per language in request order.
- **Translation status** — `GET /v3/video-translations/{id}`. `status`: `pending | running | completed | failed` (note `running`, not `processing`); `video_url` on completion, `failure_message` on failure.

## Implementation notes

- **Manifest**: `hyperreal/griptape_nodes_library.json` — underscored filename in a subdirectory named after the library, matching the official Minimax/Kling convention and current docs (the template's root-level hyphenated name is the older style). Node `file_path` entries are resolved relative to the manifest and grouped per service (`heygen/avatar_video.py`, later `dospaces/...`). `HEYGEN_API_KEY` is declared via `settings[].contents.secrets_to_register`.
- **Self-contained node files, no shared client module**: every official library (Minimax, Kling, ElevenLabs) duplicates its HTTP helpers per node file because the engine loads each node file individually — cross-file imports between library files are unproven in this ecosystem. The ~80 duplicated lines are the deliberate trade.
- **Media input handling**: artifact values arrive in many forms depending on where they came from — raw bytes, `http(s)://` URLs, `data:` URIs, workspace-relative paths, and `{project_dir}/...` macro paths (project-based workflows). The nodes resolve all of them; macros go through the engine's `ParsedMacro` + `GetPathForMacroRequest` API (lazy-imported so the library still loads on older engines).
- **Output persistence**: results are downloaded and saved via `StaticFilesManager` (durable localhost URLs), optionally also written to `output_directory`.
- **Engine pin**: `engine_version: 0.86.0` — verified by installing that engine and instantiating both nodes against it.

## Gotchas

- **New secrets need an engine restart.** `secrets_to_register` fires on `app_events.on_app_initialization_complete`, so a newly added secret (like `HEYGEN_API_KEY` on first install) is only registered at engine startup — *Refresh Libraries is not enough*. Node **code** changes, by contrast, only need a refresh.
- **HeyGen video URLs are presigned and expire.** That's why the nodes download every result and re-host it via static files (and optionally `output_directory`) instead of passing HeyGen's URL downstream. Never store a raw HeyGen `video_url` for later use.
- **Language identifiers are display names, verified live**: `"Catalan"` (plain — the region-suffixed `"Catalan (Spain)"` form appears in HeyGen's help pages, but the live check accepted and returned plain `"Catalan"`) and `"Spanish (Spain)"`. The node's submit-time validation against `GET /v3/video-translations/languages` catches wrong spellings with suggestions.
- **API credits are a separate pool from subscription credits.** A funded HeyGen subscription still yields `MOVIO_PAYMENT_INSUFFICIENT_CREDIT` until API credits are purchased for the key's account. Higher resolution consumes credits faster.
- **32 MB upload cap** on `/v3/assets` applies to the image, the audio, and any locally-hosted source video the translate node has to re-upload.
- **Never enable Topaz `frame_interpolation` on a head crop that will be composited back.** Composite Region Back requires the insert's frame count to exactly match the region's — interpolation changes it, and the node will (correctly) refuse. Upscale only.
- **WaveSpeed's upload API returns `data.download_url`, not the documented `data.url`.** Discovered live 2026-08-05; the nodes accept both, but keep it in mind when writing new WaveSpeed nodes.
- **Auto-versioned save filenames use `{_index?:03}`, not `{###}`.** In Save-node `output_file` templates, the engine's version counter is the `_index` macro variable (`?` = optional so the first save resolves, `:03` = zero-pad). Save situations with the CREATE_NEW collision policy (e.g. `save_node_output`) already append it automatically on collision.
- **`DO_SPACES_ENDPOINT` is the *region* endpoint, not the bucket URL.** Use `https://atl1.digitaloceanspaces.com`, not `https://<bucket>.atl1.digitaloceanspaces.com` (the URL the DO control panel shows most prominently). With the bucket URL, boto3 appends the bucket name as a path — the upload still "works" but lands under a stray top-level folder named after the bucket, and the node's output URL doubles the bucket (`https://<bucket>.<bucket>.atl1...`). Verified live 2026-08-04.
- **Aliased drives break project paths.** Griptape canonicalizes paths with `Path.resolve()`, which dereferences SUBST mappings and junctions — files picked via an aliased drive (e.g. a mapped `M:`) fail the "inside the project?" check and misbehave. Inside Griptape, always use the real path; `{project_dir}` macros provide the cross-user portability a mapped drive would.

## Verification (definition of done — all confirmed 2026-08-03)

- [x] Library registers cleanly; both nodes appear in the Libraries panel
- [x] Image + audio produces an English lipsync video end to end (1080×1920 with `9:16` + `1080p`)
- [x] That video feeds directly into translate and produces Spanish + Catalan in a single call
- [x] A bad/missing API key produces a readable node error, not a stack trace
- [x] A failing single language doesn't destroy the other results (per-ID polling)
- [x] No secrets anywhere in the repo; `.env` is gitignored
- [x] README includes the "Add to Griptape Nodes" install link

## Verification — Topaz Video Upscale (upscale path confirmed live 2026-08-04)

- [x] Node appears under **HyperReal Nodes → Video → Topaz**; existing nodes still load
- [x] A HeyGen output upscales and plays with intact audio; chains into Upload to Spaces
- [ ] A frame-interpolated render (`frame_interpolation` ≠ none) verified
- [ ] Bad key and oversize input produce readable errors

## Verification — WaveSpeed nodes (Image Edit confirmed live 2026-08-05)

- [x] Nodes appear (Image → WaveSpeed, Video → WaveSpeed); existing nodes still load
- [x] An image edit with 2 reference images succeeds on `google/nano-banana-2/edit` (multi-reference character + facade composite)
- [ ] Image edit verified on `google/nano-banana-pro/edit` and `openai/gpt-image-2/edit`
- [ ] InfiniteTalk produces a talking video from image + audio; V2V from video + audio (if V2V 404s, re-check the model path — one WaveSpeed docs page spells it `infinietalk`)
- [ ] Bad key produces a readable error

## Verification — Face Prep nodes (round-trip machinery verified offline 2026-08-05; live checklist pending)

- [x] Synthetic round trip (tracked Crop → Composite, no swap): 30/30 frames, output matches the plate inside the box; frame-count-mismatched insert fails naming both counts *(offline harness)*
- [ ] All three nodes appear under **HyperReal Nodes → Video → Face Prep**; existing 7 nodes still load; **no engine restart needed** (no new secrets)
- [ ] Locked-off talking-head clip: Detect picks `static`, preview box contains hair and chin, Crop produces a square clip
- [ ] Moving-head clip: Detect picks `tracked`, cropped clip shows a stable head against a sliding background
- [ ] Two-person plate: `subject_index` selects the intended face without swapping subjects mid-clip
- [ ] Full chain with Topaz 2× in the middle produces a correctly-placed, correctly-scaled head
- [ ] Seam invisible at `feather_px=24` on a real plate

## Verification — Upload to Spaces (video path confirmed live 2026-08-04)

- [x] Library still registers cleanly; node appears under **HyperReal Nodes → Storage → Spaces**, HeyGen nodes still load
- [x] A video uploads with key prefix + custom filename and returns a working public URL
- [ ] An image and an audio file each upload and return a URL that opens in a browser
- [ ] `public=false` produces an object that is *not* publicly fetchable
- [ ] Bad credentials produce a readable node error naming the problem
- [ ] Teammates only needed an engine restart — no new library install

## Development

```bash
uv sync
uv run ruff check hyperreal
```

Library manifest: [hyperreal/griptape_nodes_library.json](hyperreal/griptape_nodes_library.json). The DigitalOcean Spaces node was built from the spec in [SPEC.md](SPEC.md).
