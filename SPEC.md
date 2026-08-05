# SPEC: DigitalOcean Spaces + Topaz + WaveSpeed nodes (HyperReal Nodes library)

**Repo:** `production-hr/griptape-nodes-library-hyperreal` — this repo; the dospaces nodes are an **addition to the existing library**, not a new one.
**Status:** Draft for implementation.

The HeyGen nodes **shipped on 2026-08-03** — as-built documentation, API contract, implementation notes, and gotchas are in [README.md](README.md). Per the 2026-08-03 decision, all HyperReal custom nodes live in this single "HyperReal Nodes" library (one install and one panel section for the team), organized by category. This document specifies the next category: the **DigitalOcean Spaces** upload utility, needed by ViewComfy (whose APIs require publicly fetchable input URLs, unlike HeyGen's direct-upload asset API) and useful for archiving deliverables.

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

## 6. Reference

- As-built reference implementation: [README.md](README.md) and `hyperreal/heygen/avatar_video.py` (input handling, SuccessFailureNode wiring, manifest shape)
- DO Spaces S3 compatibility: `docs.digitalocean.com/products/spaces/` (verify the current endpoint URL format for the chosen region before hard-coding examples in the README)
- Custom node docs: `docs.griptapenodes.com/en/stable/development/custom_nodes/` (note: the llms.txt index lists URLs without the required `/en/stable/` prefix)
