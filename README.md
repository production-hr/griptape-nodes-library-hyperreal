# HyperReal Nodes for Griptape Nodes

HyperReal's custom node library for [Griptape Nodes](https://www.griptapenodes.com/) — one library, one install, with all our service integrations organized as categories:

```
HYPERREAL NODES
├─ VIDEO
│  └─ HEYGEN            HeyGen Avatar Video · HeyGen Video Translate
├─ STORAGE (planned)
│  └─ SPACES            Upload to Spaces — see SPEC.md
└─ VIEWCOMFY (planned)
```

Current contents: **HeyGen** nodes — generate lipsync avatar videos from an image + audio (Avatar IV), and translate videos into other languages with lip-sync and voice preservation.

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
2. **Set your API key**: Settings → API Keys & Secrets → `HEYGEN_API_KEY` (get one from the [HeyGen dashboard](https://app.heygen.com/settings?nav=API)), then **restart the engine** — see Gotchas.
3. Refresh Libraries. The nodes appear under **HyperReal Nodes → Video → HeyGen**.

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
- **Aliased drives break project paths.** Griptape canonicalizes paths with `Path.resolve()`, which dereferences SUBST mappings and junctions — files picked via an aliased drive (e.g. a mapped `M:`) fail the "inside the project?" check and misbehave. Inside Griptape, always use the real path; `{project_dir}` macros provide the cross-user portability a mapped drive would.

## Verification (definition of done — all confirmed 2026-08-03)

- [x] Library registers cleanly; both nodes appear in the Libraries panel
- [x] Image + audio produces an English lipsync video end to end (1080×1920 with `9:16` + `1080p`)
- [x] That video feeds directly into translate and produces Spanish + Catalan in a single call
- [x] A bad/missing API key produces a readable node error, not a stack trace
- [x] A failing single language doesn't destroy the other results (per-ID polling)
- [x] No secrets anywhere in the repo; `.env` is gitignored
- [x] README includes the "Add to Griptape Nodes" install link

## Development

```bash
uv sync
uv run ruff check hyperreal
```

Library manifest: [hyperreal/griptape_nodes_library.json](hyperreal/griptape_nodes_library.json). The next additions (DigitalOcean Spaces nodes) are specified in [SPEC.md](SPEC.md).
