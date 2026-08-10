# SPEC: DigitalOcean Spaces + Topaz + WaveSpeed + Face Prep + Composite nodes (HyperReal Nodes library)

**Repo:** `production-hr/griptape-nodes-library-hyperreal` — this repo; the dospaces nodes are an **addition to the existing library**, not a new one.
**Status:** Draft for implementation.

The HeyGen nodes **shipped on 2026-08-03** — as-built documentation, API contract, implementation notes, and gotchas are in [README.md](README.md). Per the 2026-08-03 decision, all HyperReal custom nodes live in this single "HyperReal Nodes" library (one install and one panel section for the team), organized by category. This document specifies the categories that follow, each shippable independently: **DigitalOcean Spaces** (§2–3), **Topaz** (§4), **WaveSpeed** (§5), **Face Prep** (§6), and **Composite** (§7).

---

## 0. Scope

One node: upload a media artifact to a DO Spaces bucket and return its public URL. This is the piece that turns any locally-generated asset into something an external API can fetch — the gap the local-engine architecture otherwise has.

## 1. How it lands in this repo (conventions verified by the HeyGen build)

- New directory `hyperreal/dospaces/` with a **self-contained** node file `upload_to_spaces.py` (no shared client module — the engine loads node files individually; copy helpers from `hyperreal/heygen/avatar_video.py` rather than importing them).
- Manifest additions to the existing `hyperreal/griptape_nodes_library.json`:
  - Category: `"storage/spaces"` (renders as STORAGE → SPACES in the panel).
  - Node entry with `file_path: "dospaces/upload_to_spaces.py"` (manifest-relative).
  - `boto3` appended to `metadata.dependencies.pip_dependencies`.
  - Four new secrets appended to `secrets_to_register` (see §2). **Gotcha:** secret registration fires on `app_events.on_app_initialization_complete` — teammates must restart the engine after this update, not just Refresh Libraries.
- **Base class `SuccessFailureNode`**: `_create_status_parameters()` in the constructor, `_clear_execution_status()` at process start, `_set_status_results(...)` on completion, `_handle_failure_exception(e)` in the except block. Async via `process()` yielding `lambda: self._process()`.
- **Media input handling**: copy `_artifact_to_bytes` / `_resolve_workspace_path` / `_resolve_macro_path` from the HeyGen nodes verbatim — artifact values arrive as raw bytes, `http(s)` URLs, `data:` URIs, workspace-relative paths, or `{project_dir}` macros, and all forms must work.
- Version: bump `metadata.library_version` (and pyproject) to 0.2.0 on ship.

## 2. Node: `Upload to Spaces` (`UploadToSpaces`)

DO Spaces is S3-compatible — use `boto3` with a custom `endpoint_url`, not any DO-specific SDK.

**Inputs**

| Parameter | Type | Mode | Notes |
|---|---|---|---|
| `artifact` | ImageArtifact / ImageUrlArtifact / AudioArtifact / AudioUrlArtifact / VideoUrlArtifact | INPUT | The asset to upload |
| `bucket` | str | INPUT/PROPERTY | Spaces bucket name |
| `key_prefix` | str | INPUT/PROPERTY | e.g. `gaudi/welcome/`; final key is `<prefix><filename>` |
| `filename` | str | INPUT/PROPERTY | Optional override; default derived from the artifact name or content-sniffed extension |
| `public` | bool | PROPERTY | Default true — sets `ACL: public-read`; ViewComfy needs public URLs |

**Outputs**

| Parameter | Type | Notes |
|---|---|---|
| `url` | str | `https://<bucket>.<region>.digitaloceanspaces.com/<key>` (or CDN endpoint if configured) |
| `key` | str | The object key, for downstream deletes/replacements |

Plus the standard `was_successful` / `result_details` and Succeeded/Failed control outputs.

**Secrets:** `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `DO_SPACES_REGION`, `DO_SPACES_ENDPOINT` — all four registered in the manifest.

**Process**

1. Read secrets; fail with a readable error naming the missing one(s).
2. Resolve the artifact to bytes (shared input-handling helpers). Sniff content type from magic bytes for the upload's `ContentType` — correct `ContentType` matters for browser/API consumers.
3. `boto3.client("s3", region_name=..., endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=...)` → `put_object(Bucket=..., Key=..., Body=..., ContentType=..., ACL="public-read" if public else "private")`.
4. Emit `url` and `key`; put the byte size and target in `result_details`.

**Failure policy:** wrong credentials, missing bucket, and network errors must each produce a distinct readable message (boto3's `ClientError` carries an error code — surface it).

## 3. Definition of done

- [ ] Library still registers cleanly; node appears under **HyperReal Nodes → Storage → Spaces**, and the HeyGen nodes still load
- [ ] An image, an audio file, and a video each upload and return a URL that opens in a browser
- [ ] `public=false` produces an object that is *not* publicly fetchable
- [ ] Bad credentials produce a readable node error naming the problem
- [ ] No secrets in the repo; README documents the four secrets and the engine-restart gotcha
- [ ] Teammates need only restart the engine — no new library install

## 4. Node: `Topaz Video Upscale` (`TopazVideoUpscale`)

Upscale a video (e.g. HeyGen 1080p output → 4K deliverable) via the Topaz Labs Video API. Contract verified against developer.topazlabs.com on 2026-08-04.

- Directory `hyperreal/topaz/video_upscale.py`, category `"video/topaz"` (VIDEO → TOPAZ beside HEYGEN). Secret: `TOPAZ_API_KEY`. No new pip deps (`requests` only).
- **API flow (express mode — no source metadata needed):**
  1. `POST https://api.topazlabs.com/video/express` with `X-API-Key`, body `{source: {container}, filters: [{model}], output: {resolution: {width, height}, frameRate, audioCodec: "AAC", audioTransfer: "Copy", videoEncoder, dynamicCompressionLevel: "High", container: "mp4"}}` → `{requestId, uploadUrls}`. Creating the request doesn't bill; the **lower bound** of the cost estimate is billed on processing.
  2. HTTP `PUT` the video bytes to `uploadUrls[0]` (500 MB request cap).
  3. Poll `GET /video/{requestId}/status` — statuses `requested … processing … complete | failed | canceled`; `progress` %, `estimates.cost` [low, high] credits, failure text in `message`.
  4. On `complete`, download `download.url` **within its TTL (24 h)** and re-host via StaticFilesManager (+ optional `output_directory`), like the HeyGen nodes.
- **Models** (dropdown): `prob-4` Proteus (default — general upscaling), `iris-3` Iris (face recovery, suits talking-head avatars), `ahq-12` Artemis HQ, `nyx-3` Nyx (denoise). Filters are sent with the model id only — Topaz's auto-parameter mode tunes the rest.
- **Frame interpolation** (`frame_interpolation` dropdown, default `none`): appends a second filter `{model, fps: output_frame_rate}` to the same request — `chr-2`/`chf-3` Chronos (rate conversions, 24↔25↔30), `apo-8`/`apf-2` Apollo (big multipliers, →60/slow-mo). Contract verified from the Chronos/Apollo model pages 2026-08-04; `fps` range 15–240. One job = one upload/encode/billing pass, which is why this lives in the upscale node instead of a separate node.
- **Inputs:** `video` (VideoUrlArtifact, all the usual value forms), `model`, `output_width`/`output_height` (ints, default 3840×2160; swap for portrait, e.g. 2160×3840 for the 9:16 welcome videos), `output_frame_rate` (int, default 25 — HeyGen outputs 25 fps; match the source to avoid resampling), `video_encoder` (`H264` default / `H265`), `output_directory` (optional, `{project_dir}` macros OK).
- **Outputs:** `video` (VideoUrlArtifact re-hosted via static files), `request_id` (str), plus standard status params.
- **Failure policy:** 401 → readable "check TOPAZ_API_KEY"; `failed` status surfaces the API's `message`; poll timeout 60 min with backoff; oversize input rejected before upload.

### Definition of done (Topaz)

- [ ] Node appears under **HyperReal Nodes → Video → Topaz**; existing nodes still load
- [ ] A HeyGen 1080p output upscales to 4K and plays
- [ ] Bad key and oversize input produce readable errors
- [ ] README documents `TOPAZ_API_KEY` and billing/experience notes (first request free; lower-bound estimate billed)

## 5. WaveSpeed nodes (`hyperreal/wavespeed/`)

Three nodes over the WaveSpeed AI platform (contract verified against wavespeed.ai/docs 2026-08-04), grouped **by input signature, not vendor**. Secret: `WAVESPEED_API_KEY`. Version bump to 0.3.0.

**Shared API skeleton:** `POST https://api.wavespeed.ai/api/v3/<model-path>` (Bearer auth, JSON body, media as URLs) → `data.id`; poll `GET /api/v3/predictions/{id}/result` (2s→5s images, 5s→10s video) until `completed`/`failed`/`cancelled`/`timeout`; outputs at `data.outputs` (URL array), error text at `data.error`. Local/localhost artifacts are first uploaded via `POST /api/v3/media/upload/binary` (multipart `file`, 200 MB cap, 7-day retention) → `data.url`; already-public http(s) URLs pass through untouched. Results are re-hosted via StaticFilesManager (+ optional `output_directory`), like all our nodes.

1. **`WaveSpeedImageEdit`** (`wavespeed/image_edit.py`, category `image/wavespeed`) — model dropdown `google/nano-banana-pro/edit` (default) / `google/nano-banana-2/edit` / `openai/gpt-image-2/edit`; `prompt` (multiline), `images` ParameterList (≤14 google, ≤16 gpt — validated per model), `aspect_ratio` (auto default → omitted from body; union of the three models' common ratios), `resolution` 1k/2k/4k, `quality` low/medium/high (sent **only** for gpt-image-2), `output_format` png/jpeg. Outputs `image` (ImageUrlArtifact) + `prediction_id`.
2. **`WaveSpeedInfiniteTalk`** (`wavespeed/infinitetalk.py`, category `video/wavespeed`) — model path `wavespeed-ai/infinitetalk`: `image` + `audio` (required), optional `prompt`, `mask_image`, `resolution` 480p (default)/720p, `seed` (-1 random). Outputs `video` + `prediction_id`. ($0.03/s at 480p, $0.06/s at 720p, $0.15 min.)
3. **`WaveSpeedInfiniteTalkV2V`** (`wavespeed/infinitetalk_v2v.py`, category `video/wavespeed`) — model path `wavespeed-ai/infinitetalk/video-to-video`: same as above but `video` replaces `image`. **Note:** one docs page spelled the path `infinietalk` — assumed typo; if the first live run 404s, check the path.

### Definition of done (WaveSpeed)

- [ ] All three nodes appear (Image → WaveSpeed, Video → WaveSpeed); existing nodes still load
- [ ] An image edit with 2+ reference images succeeds on each of the three models
- [ ] InfiniteTalk produces a talking video from image + audio; V2V from video + audio
- [ ] Bad key produces a readable error; README documents `WAVESPEED_API_KEY` + engine restart

## 6. Face Prep nodes (`hyperreal/faceprep/`)

Three nodes that bracket an external face-swap step: isolate the head from a video as a square crop, and later paste the swapped result back onto the plate. The point is resolution — swapping against a 512–1024 px head instead of a head that occupies 15% of a 1080p frame. Between the two halves sits `TopazVideoUpscale` (§4) and, for now, a manual face-swap pass outside Griptape.

**Pipeline shape:** `Load Video → Detect Head Region → Crop To Region → Topaz Video Upscale → [external face swap] → Composite Region Back → Save Video`

New category `video/faceprep` → title "Face Prep", `border-amber-500`, icon Video. Version bump to **0.4.0**. New pip deps: `opencv-python`, `numpy`, `static-ffmpeg>=2.8` (appended to both `pyproject.toml` and `metadata.dependencies.pip_dependencies`). **No new secrets** — nothing here is a remote call, so this is the first update that needs only Refresh Libraries, not an engine restart.

### 6.1 Conventions (unchanged from §1, plus three new ones)

- All three nodes extend `SuccessFailureNode`, self-contained files, helpers duplicated per file, `_artifact_to_bytes` copied verbatim — same as every existing node.
- **`param_types` does not exist on 0.86.0** — use `Parameter(type=...)` + the `Options` trait, as the shipped nodes do. (Note: the *standard* library's `main` imports `griptape_nodes.exe_types.param_types` and `griptape_nodes.files.file.File`, so it targets 0.94.x. Don't copy its parameter style into this repo until the repo's pin moves.)
- **ffmpeg**: `static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()` returns `(ffmpeg_path, ffprobe_path)` — same call the standard video nodes use. First run downloads the binaries.
- **Detector weights**: vendor `face_detection_yunet_2023mar.onnx` (~340 KB) at `hyperreal/faceprep/models/`. Nothing downloads at node-run time, and the library stays reproducible from git — which matters for the virtual-desktop migration. *Confirm the model's redistribution terms before committing it; if that's not clean, fall back to fetch-on-first-use cached under the workspace.*

### 6.2 The `region` contract

The artifact that makes the two halves invertible. Emitted by Detect, consumed by Crop and Composite, passed through unchanged so it survives a graph edit.

```json
{
  "schema": "hyperreal.head_region/1",
  "source":   { "width": 1920, "height": 1080, "frame_rate": 25.0, "frame_count": 250 },
  "box":      { "x": 704, "y": 96, "width": 512, "height": 512, "confidence": 0.94 },
  "mode":     "static",
  "offsets":  [[704, 96], [705, 96], ...],
  "detector": "yunet-2023mar",
  "notes":    { "drift_px": 12, "detection_interval": 5, "frames_missed": 0, "clamped": false }
}
```

`box` deliberately uses `x` / `y` / `width` / `height` / `confidence` — the same key names the ecosystem's face-detection nodes emit — so the stock **Add Bounding Boxes**, **Get Dictionary Value by Key**, and **Crop Image** nodes can consume it directly, and so a future first-party detector could substitute for ours. `offsets` is per-frame top-left origins, always present (length == `frame_count`); in `static` mode every entry is identical, which lets Crop and Composite use one code path. Box **size never varies within a clip** — only position — so the crop is a pure pixel copy and paste-back needs no inverse scaling.

Type is `"json"` (the same param type `HeyGenVideoTranslate.language_map` already uses).

### 6.3 `DetectHeadRegion` — "Detect Head Region" — `video/faceprep`

| name | type / input_types | modes | default | notes |
|---|---|---|---|---|
| video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | |
| head_scale | float / [float] | I,P | 1.6 | face box → head box expansion; 1.6 typically clears hair and jaw |
| subject_index | int / [int] | I,P | 0 | 0 = highest-confidence face; for multi-person plates |
| box_mode | str | P | "auto" | Options: auto, static, tracked |
| snap_multiple | int | P | 64 | final box dims snapped up to a multiple of this |
| detection_interval | int / [int] | I,P | 5 | detect every Nth frame, interpolate between |
| confidence_threshold | float / [float] | I,P | 0.6 | |
| *(advanced group, collapsed)* pad_top / pad_bottom / pad_sides | float | P | 0.9 / 0.5 / 0.35 | fractions of face height/width; override `head_scale` when non-zero |
| *(advanced)* smoothing_window | int | P | 9 | frames; moving average over offsets before rounding to int |
| region | out: json | O | — | §6.2 |
| x / y / width / height | out: int | O | — | flat convenience outputs — wire straight into the stock **Crop Video** node |
| preview_video | out: VideoUrlArtifact | O | — | box drawn on the plate; this is how you eyeball a bad detection |

**Process**

1. `cv2.VideoCapture` for dimensions, fps, frame count; cross-check frame count with `ffprobe` and prefer ffprobe's when they disagree (cv2's `CAP_PROP_FRAME_COUNT` is unreliable on VFR sources).
2. Every `detection_interval`-th frame: `cv2.FaceDetectorYN` → face boxes. Pick the subject by confidence rank on the first hit, then by nearest centroid to the previous pick on every subsequent hit — otherwise a two-person shot swaps subjects mid-clip.
3. Face box → head box: expand by `pad_*` about the face centre (top gets the most, for hair), square it, snap up to `snap_multiple`.
4. Box size for the clip = max required size across all samples, clamped to source dimensions (set `notes.clamped` if clamping happened, and say so in `result_details` — it means the head leaves frame).
5. Offsets: linear-interpolate between sampled frames, moving-average smooth over `smoothing_window`, round to int, clamp so the box stays inside the frame.
6. `auto` mode picks: union box area ≤ 1.15× the largest single-frame box area → `static` (offsets all equal to the union origin); otherwise `tracked`. Record measured drift in `notes.drift_px` so a surprising choice is diagnosable.
7. Failure policy: zero faces found in any sampled frame → fail with the frame count sampled and the threshold used. Faces found in some frames but not others → interpolate across the gap, record `frames_missed`, succeed with a warning in `result_details`.

### 6.4 `CropToRegion` — "Crop To Region" — `video/faceprep`

| name | type / input_types | modes | default | notes |
|---|---|---|---|---|
| video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | the plate |
| region | json / [json] | I | — | from Detect |
| crf | int / [int] | I,P | 12 | well above the standard library's CRF 18 ceiling — this is a swap intermediate, not a deliverable |
| pix_fmt | str | P | "yuv420p" | Options: yuv420p, yuv444p. 4:4:4 preserves chroma into the swap but High 4:4:4 Predictive chokes some tools — try 420 first |
| output_directory | str / [str] | I,P | "" | macro-aware, as everywhere |
| head_video | out: VideoUrlArtifact | O | — | audio stripped (`-an`) |
| region | out: json | O | — | passed through unchanged |

**Process** — two paths off `region.mode`:

- **static**: single ffmpeg pass, `-vf crop=w:h:x:y`, `-c:v libx264 -preset slow -crf <crf> -pix_fmt <pix_fmt> -an`.
- **tracked**: decode to `rawvideo`/`bgr24` on a pipe, slice each frame with numpy at that frame's offset, pipe to an ffmpeg encoder with the same settings. Streamed frame by frame — never accumulate the clip in memory. (ffmpeg's `crop` filter can be driven per-frame via `sendcmd`, but the pipe is deterministic, and the composite node needs the same machinery anyway.)

Integer offsets mean the crop is a pure pixel copy in both paths — no resampling, no interpolation kernel, nothing to soften the face before the swap sees it.

### 6.5 `CompositeRegionBack` — "Composite Region Back" — `video/faceprep`

| name | type / input_types | modes | default | notes |
|---|---|---|---|---|
| plate_video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | the original |
| insert_video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | swapped (and upscaled) head |
| region | json / [json] | I | — | from Detect or Crop |
| mask_video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | optional; white-on-black matte at insert resolution |
| feather_px | int / [int] | I,P | 24 | at box scale |
| mask_shrink_px | int / [int] | I,P | 8 | erode before feathering, so the blend edge sits *inside* the detected region |
| edge_shape | str | P | "ellipse" | Options: ellipse, rounded_rect — ignored when `mask_video` is connected |
| color_match | bool | P | false | mean/std match of the insert to the plate region in LAB, per frame |
| audio_source | str | P | "plate" | Options: plate, none |
| crf | int / [int] | I,P | 16 | |
| output_directory | str / [str] | I,P | "" | |
| composited_video | out: VideoUrlArtifact | O | — | name differs from inputs (a node can't reuse a param name) |

**Process**

1. **Validate before doing any work** — fail loudly, never conform silently:
   - insert frame count ≠ `region.source.frame_count` → fail naming both numbers. This is the failure mode that matters: Topaz frame interpolation or a swap tool that drops a frame will otherwise desync progressively and look like a tracking bug.
   - insert fps ≠ plate fps → fail.
   - plate dimensions ≠ `region.source` → fail.
2. Resize the insert to `region.box.width/height` with Lanczos — downscale from the upscaled resolution, which is the safe resampling direction.
3. Build the alpha: from `mask_video` if connected (erode by `mask_shrink_px`, blur by `feather_px`), else a procedural ellipse/rounded-rect with the same erode-then-blur.
4. Composite per frame at that frame's offset, streaming through the same rawvideo pipe as §6.4.
5. Remux the plate's audio with `-c:a copy` when `audio_source == "plate"`.

`mask_video` is the seam between this and the ecosystem: when a rectangle-ish feather isn't enough (hair against a busy background), generate a matte with the **SAM3** or **G-DINO + SAM2** video node and wire it in. Not required for the first pass.

### 6.6 Definition of done

- [ ] All three nodes appear under **HyperReal Nodes → Video → Face Prep**; the existing 7 nodes still load; **no engine restart needed** (no new secrets)
- [ ] A locked-off talking-head clip: Detect chooses `static`, preview shows the box containing hair and chin, Crop produces a square clip
- [ ] A moving-head clip: Detect chooses `tracked`, and the cropped clip shows a stable head against a sliding background
- [ ] A two-person plate: `subject_index` selects the intended face and doesn't swap subjects mid-clip
- [ ] Round trip with no swap in the middle (Crop → Composite) is visually indistinguishable from the plate inside the box, and the seam is invisible at `feather_px=24`
- [ ] Full chain with Topaz 2× in the middle produces a correctly-placed, correctly-scaled head
- [ ] A deliberately frame-count-mismatched insert fails with a readable error naming both counts
- [ ] README documents the `region` schema, the new pip deps, the vendored ONNX, and the **do not enable Topaz frame interpolation on a head crop** rule

### 6.7 Deferred

- **Roll stabilisation.** YuNet returns five landmarks; rotating the crop to level the eyes would help swap quality on tilted-head shots, at the cost of a rotation in the sidecar and a resample on both legs. Not in v1.
- **Detector substitution.** Griptape demoed a YOLOv8 face-detection node (Ultralytics) in the advanced-media library that emits exactly this dict shape, but it is **not present in that library's `main` as of 2026-08-05** (28 nodes at v0.73.0; the detectors shipped are OpenPose, G-DINO+SAM2, Canny, Anyline). If it lands, consider deferring to it — but note Ultralytics is AGPL-3.0, which is why v1 uses YuNet.
- **Batch/queue handling.** Out of scope per the standing 1–5 ad-hoc jobs rule.

## 7. Composite nodes (`hyperreal/composite/`)

One node that replaces the background behind a full-frame subject. Built for the HeyGen case — Avatar IV animates the *whole* source image, so a static background won't stay static — but deliberately vendor-agnostic: it takes a foreground video, a background, and a way of deriving alpha, and it doesn't know or care where the footage came from.

**Pipeline shape:** `Load Image (subject on green) + Load Audio → HeyGen Avatar Video → Composite Over Background ← Load Image (background plate)`

New category `video/composite` → title "Composite", `border-cyan-500`, icon Layers (verify the icon name resolves; Face Prep uses Video). Version bump to **0.5.0**. **No new pip deps** — `opencv-python`, `numpy`, and `static-ffmpeg>=2.8` all arrived with §6. **No new secrets**, so like Face Prep this is Refresh Libraries, not an engine restart.

### 7.1 Why this exists rather than the stock nodes

Checked against the standard library on 2026-08-05 and the node directory the same day: **nothing in the Griptape ecosystem pulls a chroma key.** `video/add_overlay.py` is the only video compositor and it's the wrong shape twice over — its *base* input must be a video (our background is a still, and there's no image→video node in the library) and its overlay position is hardcoded to centre (already noted in §8). `image/image_blend_compositor.py` is image-domain only. SAM3 and the advanced-media library's G-DINO+SAM2 can produce a subject matte without any greenscreen, but both are local torch models on gated weights — wrong fit for a laptop engine. They remain the escape hatch via `external` mode (§7.3).

### 7.2 Naming

`CompositeOverBackground`, not `ChromaKeyComposite` — keying is only one of four ways this node gets its alpha, and the name parallels §6.5's `CompositeRegionBack`. File `hyperreal/composite/composite_over_background.py`, display name "Composite Over Background".

### 7.3 The `matte_source` discriminator

The one parameter that decides everything else. Four modes, one compositing tail:

| mode | alpha comes from | when |
|---|---|---|
| `key_auto` *(default)* | key colour sampled from the footage itself | normal path; survives HeyGen shifting the green |
| `key_manual` | the `key_color` you supply | repeatable runs; subject standing in a sampled corner |
| `external` | `matte_video`, a black-and-white clip | the key failed and you fixed it in Resolve/AE — or you generated a matte with SAM3 / G-DINO+SAM2 |
| `embedded` | the foreground's own alpha channel | source is already a webm/ProRes 4444 with alpha; no key at all |

`embedded` exists because HeyGen's v3 `CreateVideoFromImage` schema accepts `output_format: "webm"` (real alpha) and `remove_background` — **schema-verified 2026-08-05, behaviour on raw-image input unverified.** If those turn out to work, the greenscreen leg becomes unnecessary and this node still does the comp. Test that before committing to a green pipeline.

**Matte polarity: white = opaque, black = transparent.** Stated explicitly because the ecosystem isn't consistent — the VOID library uses white = *remove*. `invert_matte` makes a wrong-polarity export a checkbox, not a re-render.

### 7.4 `CompositeOverBackground` — "Composite Over Background" — `video/composite`

| name | type / input_types | modes | default | notes |
|---|---|---|---|---|
| foreground_video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | the subject; keyed, matted, or already alpha |
| background | ImageArtifact / ImageUrlArtifact / VideoUrlArtifact | I | — | still plate or moving background |
| matte_source | str | P | "key_auto" | Options: key_auto, key_manual, external, embedded |
| key_color | str / [str] | I,P | "#00B140" | used by `key_manual`; ignored elsewhere. Mid-saturation digital green — `#00FF00` clips and spills hard on hair |
| key_algorithm | str | P | "chromakey" | Options: chromakey, colorkey. `chromakey` keys on UV and ignores luma, so shading and lighting drift cost nothing; `colorkey` is RGB, for flat graphics |
| similarity | float / [float] | I,P | 0.25 | **not** the 0.10–0.15 you'd use on real greenscreen — generated footage is looser |
| blend | float / [float] | I,P | 0.10 | |
| matte_video | VideoUrlArtifact / [VideoUrlArtifact] | I | — | `external` only; luma is read as alpha |
| invert_matte | bool / [bool] | I,P | false | for black = opaque sources |
| despill | bool / [bool] | I,P | true | independent of `matte_source` — a green plate spills whether or not the matte came from elsewhere |
| *(advanced)* despill_amount | float / [float] | I,P | 0.5 | |
| *(advanced)* matte_erode_px | int / [int] | I,P | 0 | erode before feather, so the blend edge sits inside the subject — same technique as §6.5 |
| *(advanced)* matte_feather_px | int / [int] | I,P | 0 | |
| background_fit | str | P | "cover" | Options: cover, contain, stretch |
| audio_source | str | P | "foreground" | Options: foreground, background, none |
| output_alpha | bool | P | false | skip compositing entirely; emit VP9/`yuva420p` webm. Makes the node a standalone keyer — `background` is ignored |
| crf | int / [int] | I,P | 16 | matches §6.5 |
| output_directory | str / [str] | I,P | "" | macro-aware |
| composited_video | out: VideoUrlArtifact | O | — | name differs from inputs (a node can't reuse a param name) |
| matte | out: VideoUrlArtifact | O | — | the alpha as black-and-white video. **Wire this to a Display Video.** Tuning `similarity`/`blend` by eyeballing the comp is miserable; this is also the file you take into Resolve, fix, and feed back as `matte_video` |
| detected_key_color | out: str | O | — | what `key_auto` measured; paste into `key_color` + `key_manual` for a repeatable run |

### 7.5 Process

1. `static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()` → `(ffmpeg_path, ffprobe_path)`, as §6.1.
2. ffprobe the foreground: width, height, `avg_frame_rate`, `nb_frames`, `pix_fmt`.
3. Resolve alpha per `matte_source`:
   - **embedded** — assert `pix_fmt` carries alpha (`yuva*`, `rgba`, `bgra`, `argb`). If not, fail naming the actual `pix_fmt`; this is the failure you'll hit when HeyGen silently returns mp4 after ignoring `output_format`.
   - **key_auto** — sample `key_sample_frames` (5) evenly across the clip with cv2. At each, take four 32×32 patches inset 5% from the corners. **Reject any patch whose per-channel stddev exceeds `key_sample_tolerance` (12)** — that's the subject or a gradient intruding, not backing. Median the accepted patches → hex. All rejected → fail, and say to use `key_manual`. Emit the result as `detected_key_color`.
   - **key_manual** — parse `key_color`, failing readably on a malformed hex.
   - **external** — ffprobe `matte_video` and **validate before doing any work**: frame count ≠ foreground's → fail naming both numbers; fps ≠ foreground's → fail naming both. Resolution mismatch is fine, scale it. This mirrors §6.5's validation for the same reason — a matte off by two frames is subtly wrong everywhere and maddening to diagnose, and `shortest=1` would paper over it silently.
4. Background: if it's a still, `-loop 1 -i` and `-shortest`. If it's a video **shorter than the foreground, fail naming both durations** — don't truncate the performance to fit the plate.
5. Build the filtergraph (§7.6), run one ffmpeg invocation, `-map` both the comp and the matte out of it so there's a single decode/encode pass.
6. Audio: `-map <fg>:a?` / `-map <bg>:a?` / omit, per `audio_source`.
7. Emit both videos via StaticFilesManager (+ optional `output_directory`), like every other node here.

### 7.6 Filtergraphs

Input order is `0` background, `1` foreground, `2` matte (external only). `W`/`H` are the foreground's dimensions — the foreground never gets resampled, the background conforms to it.

**Key path** (`key_auto` / `key_manual`):

```
[1:v]chromakey=<hex>:<similarity>:<blend>,despill=type=green:mix=<amount>:expand=0[fg];
[fg]split[fga][fgb];
[fga]alphaextract,erosion,gblur=sigma=<feather>[m];
[fgb][m]alphamerge[fgm];
[0:v]scale=W:H:force_original_aspect_ratio=increase,crop=W:H,setsar=1[bg];
[bg][fgm]overlay=shortest=1:format=auto[v];
[fgm]alphaextract[matteout]
```

**External matte path:**

```
[2:v]format=gray,scale=W:H[m0];
[1:v]despill=type=green:mix=<amount>:expand=0[fg];
[fg][m0]alphamerge[fgm];
[0:v]scale=W:H:force_original_aspect_ratio=increase,crop=W:H,setsar=1[bg];
[bg][fgm]overlay=shortest=1:format=auto[v];
[m0]null[matteout]
```

`alphamerge` reads the **luma** of its second input, which is why an h264 matte is fine — luma is full resolution even under 4:2:0, so the delivery codec of your fixed matte costs you nothing. `invert_matte` inserts `negate` after `format=gray`. `matte_erode_px`/`matte_feather_px` drop out of the graph when zero. `background_fit`: `cover` as written, `contain` swaps to `decrease` + `pad`, `stretch` is a bare `scale=W:H`.

**Encode:** `-c:v libx264 -preset slow -crf <crf> -pix_fmt yuv420p`. With `output_alpha`, skip inputs `0` and the overlay entirely and encode `[fgm]` as `-c:v libvpx-vp9 -pix_fmt yuva420p -crf <crf> -b:v 0` into a `.webm`.

### 7.7 Definition of done

- [ ] Node appears under **HyperReal Nodes → Video → Composite**; the existing 10 nodes still load; **no engine restart needed**
- [ ] A HeyGen green-background clip composites onto a still plate, and the subject's audio survives
- [ ] `key_auto` on that clip reports a `detected_key_color` within a few percent of the source green — and the comp holds up when the same subject is re-run and the green shifts
- [ ] `matte` output opens in Display Video and visibly responds to `similarity` / `blend`
- [ ] Round trip: take the `matte` output, feed it straight back as `matte_video` in `external` mode, get a visually identical comp
- [ ] An inverted matte plus `invert_matte=true` matches the non-inverted result
- [ ] A frame-count-mismatched `matte_video` fails with a readable error naming both counts
- [ ] `embedded` mode on an alpha webm composites without keying; `embedded` on a plain mp4 fails naming the `pix_fmt`
- [ ] A moving background video works as well as a still
- [ ] `output_alpha=true` produces a webm that carries alpha into another tool
- [ ] README documents the white = opaque convention (and the VOID contrast), the `#00B140` recommendation, and the matte round-trip workflow

### 7.8 Source-image guidance (for the README, not the code)

The node can only key what it's given. For the HeyGen leg:

- **Fill the frame with green** — no floor line, no gradient, no vignette. The flatter the source, the less the model invents.
- **Generate the green-background source** with an image-edit pass (`WaveSpeedImageEdit`, §5) rather than cutting it out by hand; the result is a more uniform field.
- **Nothing green on the subject.**
- **Ask HeyGen for the highest resolution available.** A key is only as good as the pixels at the boundary, and chroma subsampling eats edge detail.
- **`expressiveness: "low"`, restrained `motion_prompt`.** Avatar IV adds camera drift; drop a drifting subject on a locked-off plate and it reads as a cutout. This is the artefact most likely to make a technically clean key look wrong.

### 7.9 Deferred

- **`matte_blend_mode`.** `multiply` against the keyed alpha gives garbage mattes and holdouts, which is the natural next ask. Ship the enum with the single value `replace` so adding it later isn't a breaking parameter change.
- **Per-frame key adaptation.** Re-sampling the key colour every N frames would track drift within a clip rather than assuming one value. Only worth it if `key_auto`'s median proves insufficient in practice.
- **Foreground transform** (scale / position / rotate). This node composites full-frame subject over full-frame background; repositioning is `image_blend_compositor`'s job in the image domain and nobody's in the video domain. Out of scope until there's a real need.
- **Batch handling.** Out of scope per the standing 1–5 ad-hoc jobs rule.

## 8. Node: `Topaz Image Upscale` (`TopazImageUpscale`)

Still-image upscale + face recovery, to sit between `Zoom To Head` and the zoomed lipsync pass in §7's two-pass trick. Contract verified against developer.topazlabs.com on 2026-08-08.

**Motivation (from live testing, not theory).** HeyGen's `1080p` means *short edge 1080* regardless of input size — measured across real outputs: portrait → 1080×1920, landscape → 1920×1080, 4:5 → 1080×1350. So the zoomed pass is already 1080p and frame size is *not* the problem. The problem is feature density in the **input**: a head crop from a full-body frame carries too few pixels across the eyes, and Avatar IV returns artifacted eyelids on blinks. Topaz face recovery adds real detail before generation. Confirmed by hand before being automated.

- Directory `hyperreal/topaz/image_upscale.py`, category `image/topaz` (new IMAGE → TOPAZ category). Secret: the existing `TOPAZ_API_KEY` — **no new secret, so no engine restart**. No new pip deps (`requests`, `cv2`, `numpy` all already present).
- **A different API surface from §4.** Async only; there is no synchronous image endpoint.
  1. `POST https://api.topazlabs.com/image/v1/enhance/async`, header `X-API-Key`, **multipart/form-data** (`image` file part + string form fields) → `{process_id}`
  2. `GET /image/v1/status/{process_id}` → `{status: Completed|Processing|Failed|Cancelled}`
  3. `GET /image/v1/download/{process_id}` → `{download_url, head_url, expiry}` — **not** `{url}`, which is what the endpoint-list page implies. GET the `download_url`; `head_url` is presigned for metadata only and an S3 signature is bound to its HTTP method, so a GET against it would 403. Link expires **1 hour** after the job completes. The node also tolerates a direct byte body, and on an unrecognised shape reports the keys it received rather than a truncated blob.
- **Models** (dropdown): `High Fidelity V2` (default — clean, already-sharp sources such as an AI-generated frame), `Standard V2`, `Low Resolution V2`, `CGI`, `Text Refine`. The generative family (`Standard MAX`, `Recovery V2`, `Wonder`, `Redefine`) lives behind `/enhance-gen/async` and is **deliberately not offered**: it invents detail freely, which is wrong for a likeness.
- **Sizing — the one real trap.** If both `output_width` and `output_height` are sent and the aspect doesn't match the source, Topaz **letterboxes**. A letterbox bar fed into a lipsync pass corrupts it silently. The node therefore exposes a single `output_long_edge`, picks `output_width` or `output_height` by source orientation, sends only that one, and lets Topaz derive the other proportionally. It then compares input vs. output aspect and warns in `result_details` if they diverge anyway.
- **Face params:** `face_enhancement` (default on — the whole point), `face_enhancement_strength` (0.8), `face_enhancement_creativity` (**0**, and documented to stay there for a real person; above 0 the likeness drifts).
- **Auto sentinel:** `denoise` / `sharpen` / `fix_compression` / `strength` default to `-1`, meaning *omit the field* so Topaz auto-tunes. Distinct from `0`, which explicitly asks for none.
- **Enum casing — found the hard way, 400 on the first live call.** `subject_detection` must be **title case**: `All` / `Foreground` / `Background`. Lowercase returns `HTTP 400: parameter "subject_detection" must be one of [All Foreground Background]`. The published docs show *both* casings on different pages, so the server is the authority. The node normalises any saved casing on the way out so older workflows keep working. `output_format` is the opposite — **lowercase**, and the accepted set is `jpeg` / `jpg` / `png` / `tiff` / `tif`, with **no webp** (webp is fine as an input). `model` strings are verbatim title case.
- **Limits enforced before upload:** 512 MP input, 1024 MP output, 500 MB request. Billed per **output** megapixel, so the guard is also a cost guard. Poll 15 min max, 2s → 10s backoff.
- **Failure policy:** 401 → readable "check TOPAZ_API_KEY"; terminal statuses surface the API message; 429 honours `Retry-After` (4 attempts); timeout surfaces `process_id` so the job can be chased.

### Definition of done (Topaz Image)

- [x] Node appears under **HyperReal Nodes → Image → Topaz**; all 14 nodes still load
- [x] 776×1380 crop + `output_long_edge` 1920 sends `output_height=1920` only, and Topaz's proportional width lands on 1079.7 ≈ 1080 — aspect `0.56232` preserved exactly
- [x] Landscape source flips to `output_width`; `output_long_edge=0` sends neither
- [x] `-1` knobs omitted, explicit `0` sent; `face_enhancement=false` drops its sub-fields; creativity clamped to 0–1
- [x] Over-limit output rejected before upload; letterbox tripwire silent on a matching aspect, warns on a changed one
- [x] `subject_detection` normalised to title case from any saved casing; `output_format` lowercased and validated with webp falling back to png
- [ ] **Live:** one real call returns a 1080×1920 png with visibly better eye detail, and the zoomed lipsync comes back without eyelid artifacting on blinks

## 9. Node: `Shot Settings` (`ShotSettings`) + the pillarbox guard

Added 2026-08-08 after a live failure: both lipsync passes were left on `aspect_ratio: auto`, HeyGen returned **16:9 for portrait inputs**, and the zoomed render came back pillarboxed. `OverlayZoomedVideo` then scaled that frame — bars included — and blended it over the plate as a dark rectangle around the face. Alignment computed correctly throughout, which is what made it insidious: nothing failed, the render was just wrong after both generations had been paid for.

Two changes, because the failure had two causes.

**1. A shared source of truth.** `hyperreal/config/shot_settings.py`, category `config/shot` (new CONFIG group). A `DataNode` — no control wiring, resolves as a dependency of whatever reads it. Five parameters, each `PROPERTY | OUTPUT`: `aspect_ratio` (default `9:16`), `resolution` (`1080p`), `expressiveness` (`low`), `upscale_long_edge` (`1920`), `output_directory` (`""`). `process()` copies properties to outputs, falling back to declared defaults so an untouched node still publishes usable values.

- **`auto` is not among the choices.** A shot's aspect is a decision; the node forces it. `auto` stays selectable on the HeyGen node so saved workflows load, but its default moved `auto` → `9:16` and its tooltip now records the measured behaviour.
- Required making `aspect_ratio` / `resolution` / `expressiveness` on `HeyGenAvatarVideo` `INPUT | PROPERTY`; they were `PROPERTY`-only and could not accept a connection at all. Widening `allowed_modes` is backwards-compatible.
- Considered and rejected: Griptape's own variables system (`Create Variable` / `Get Variable`, plus workflow-scoped `{VARIABLE_NAME}` substitution). Substitution operates on parameter *values*, and these are `Options` dropdowns — there is nowhere to type a token. It would still have needed the `INPUT` change, and then a wire, so a purpose-built node with named outputs is the smaller thing.

**2. A guard, so the same class of error cannot reach a render again.** `OverlayZoomedVideo._validate` checked frame count and frame rate but never geometry. It now refuses an overlay with bars baked in, reporting bar widths, the content aspect, the frame aspect and the base aspect, and naming `auto` as the usual cause.

- Detection is deliberately strict — a bar column must be near-zero brightness (`mean <= 12`) **and** near-zero variance (`std <= 3`), and be present in **every** sampled frame (minimum across 5 samples), so one dark frame cannot manufacture one.
- Each side is scanned no further than **45%** of the dimension. Without the cap a wholly dark frame reports bars wider than the frame itself and computes a negative content aspect — caught by the false-positive test, not by inspection.
- A pillarbox is dark bars around *brighter* content, so the check is skipped when the middle is no brighter than the edges (`centre mean <= 24`). This is what keeps a night exterior or a black backdrop from being refused.
- **Bars must be PAIRED on opposite edges** — `min(left, right)` and `min(top, bottom)`, not `max`. Padding to a different aspect is always *centred*, so a real letterbox is top **and** bottom. Shipped first with `max`, which produced a false positive on a good Ozzy render: the black throne behind his head filled the bottom of frame and measured `0px top / 71px bottom` = 3.7%, over the 2% threshold, and blocked the composite. Regression test covers a 1080×1920 clip with 71 dark rows at the bottom only.

### Definition of done (Shot Settings + guard)

- [x] `ShotSettings` publishes all five values, including defaults when untouched; 15/15 nodes still construct
- [x] HeyGen `aspect_ratio` / `resolution` / `expressiveness` accept connections; `str` → `str` is a legal link from the config node
- [x] HeyGen `aspect_ratio` default is `9:16`, with `auto` still selectable for older workflows
- [x] Synthetic 1920×1080 clip with 596 px bars → refused, message names bar widths and all three aspect ratios
- [x] Matching portrait overlay → accepted; near-black noisy content (mean ~9) → accepted, not misread as bars
- [x] **Regression:** 1080×1920 with a black chair filling the bottom of frame (`0px top / 71px bottom`) → accepted; the real 596/596 pillarbox still refused
- [ ] **Live:** Ozzy two-pass at an explicit 9:16 composites without a dark rectangle

## 10. Green plates for generated footage (resolved 2026-08-08)

Not a node — a process constraint that governs §4/§7 usage, written up in full at [docs/replica-plate-delivery.md](docs/replica-plate-delivery.md).

A green-screen plate for the Ozzy replica keyed badly: the background flickered and the key was unusable. Three approaches were considered and two were wrong.

- **Wrong: tune the keyer.** The runtime keyer measures `green − max(red, blue)` plus a brightness gate. The plate's green was `RGB(0.7, 152.2, 106.1)` → a dominance margin of **46**, with blue at 106 eating it from the side nobody was watching. Generator flicker was a large fraction of that margin. No threshold recovers it.
- **Wrong: rebuild the background downstream.** Roto the subject by identity, composite over a synthetic fill. It works, but it is a manual Resolve round trip, and a silhouette matte discards the floor contact shadow that the whole plate exists to preserve. Kept in the doc as recovery-only.
- **Right: author the green upstream, at `RGB(0, 255, 0)`.** Replace the *original white* background with pure green before the lipsync pass. Dominance margin **255** — 5.5× wider — so the same flicker stops crossing the threshold. Brightness V=255 puts the background at the ceiling, cleanly above any darkened shadow, so the gate preserves reflections. And Replace Color on flat white leaves the reflection as a *darker green*, which is exactly the form the gate wants; the shadow is preserved by construction rather than recovered.

Verified live: a clean key with the floor reflection intact.

Consequences for this library: the `remove_background` / webm-alpha test flagged in §11 is **no longer needed for this pipeline** (kept as a note, since it would still be the cheapest matte source if a future plate can't be authored upstream). Colour-matrix handling in §7's pipe-based nodes is unaffected — the shift was in the generation, not the round trip, confirmed by the user and by a BT.709 YCbCr round trip on the measured green showing zero error in both full and limited range.

## 11. Reference

- As-built reference implementation: [README.md](README.md) and `hyperreal/heygen/avatar_video.py` (input handling, SuccessFailureNode wiring, manifest shape)
- DO Spaces S3 compatibility: `docs.digitalocean.com/products/spaces/` (verify the current endpoint URL format for the chosen region before hard-coding examples in the README)
- Custom node docs: `docs.griptapenodes.com/en/stable/development/custom_nodes/` (note: the llms.txt index lists URLs without the required `/en/stable/` prefix)
- Face Prep prior art: Griptape's "face enhancement workflow" walkthrough (`youtu.be/DNjl-aNYmPQ`) — single-image, but the source of the detection-dict key names in §6.2 and the erode-mask-before-blend technique in §6.5. Its enhancement leg (FLUX Krea BLAZE via the Diffusion Pipeline Builder) is deliberately **not** adopted: local GPU, HuggingFace cache management, and a manual checkpoint file-copy step. Topaz replaces it.
- HeyGen background/matting contract: `developers.heygen.com/reference/create-video` — the `POST /v3/videos` OpenAPI spec, read 2026-08-05. The `CreateVideoFromImage` variant (the one our HeyGen Avatar Video node calls) accepts `background` (`color` | `image`, by url or asset_id), `remove_background`, and `output_format: "webm"` for a real alpha channel. **Caveat:** the prose on those fields says matting-enabled avatars are required, which is written for the `type: "avatar"` path — whether the server honours them for raw image input is unverified and worth a 15-minute test, since it would remove the need for the green leg entirely.
- Chroma key survey, 2026-08-05: no keyer exists anywhere in the standard library, the Griptape-team libraries, or the community section of `griptape-ai/griptape-nodes-directory`. Nearest neighbours are SAM3 (`griptape-nodes-library-sam3`) and the advanced-media library's G-DINO+SAM2 — both local torch on gated weights, both matte *sources* for §7's `external` mode rather than substitutes for the node.
- FFmpeg filter behaviours relied on in §7.6 (`chromakey` keys on UV and ignores luma; `alphamerge` reads its second input's luma; `despill type=green`): standard FFmpeg filter documentation. Filtergraphs in §7.6 are a **design proposal, not run-verified** — expect to tune them against real footage.
- Video-node prior art read directly from `griptape-ai/griptape-nodes-library-standard@main` on 2026-08-05: `video/base_video_processor.py` (ffmpeg resolution, CRF 18 quality ceiling), `video/crop_video.py` (static rect only), `video/add_overlay.py` (**overlay position is hardcoded to centre — not usable for paste-back**), `image/image_blend_compositor.py` (the image-domain equivalent that does support `blend_position_x/y`)
