# HeyGen Nodes for Griptape Nodes

HeyGen video nodes for [Griptape Nodes](https://www.griptapenodes.com/): generate lipsync avatar videos from an image + audio (Avatar IV), and translate videos into other languages with lip-sync and voice preservation.

[![Add to Griptape Nodes](https://img.shields.io/badge/Add%20to-Griptape%20Nodes-blue)](https://nodes.griptape.ai/#library-management?git=https://github.com/production-hr/griptape-nodes-library-heygen)

**Install link:** <https://nodes.griptape.ai/#library-management?git=https://github.com/production-hr/griptape-nodes-library-heygen>

## Target pipeline

```
approved image ─┐
                ├─► HeyGen Avatar Video ─► English lipsync video ─► HeyGen Video Translate ─► [ES, CA, ...]
approved audio ─┘
```

## Setup

1. **Install the library** with the link above, or manually: Settings → Libraries → *+ Add Library* → path to `heygen/griptape_nodes_library.json` in your clone of this repo (absolute paths work; the repo does not need to live in your workspace directory).
2. **Set your API key**: Settings → API Keys & Secrets → `HEYGEN_API_KEY` (get one from the [HeyGen dashboard](https://app.heygen.com/settings?nav=API)).
3. Refresh Libraries. Both nodes appear under the **HeyGen** category.

## Nodes

### HeyGen Avatar Video

Image + audio → English lipsync video, using HeyGen's **Avatar IV** engine (the only engine that accepts arbitrary image input — no registered avatar or digital twin needed).

| Parameter | Type | Notes |
|---|---|---|
| `image` | ImageArtifact / ImageUrlArtifact | png or jpeg, up to 32 MB |
| `audio` | AudioArtifact / AudioUrlArtifact | mp3 or wav, up to 32 MB |
| `video_title` | str | Optional; shown in the HeyGen dashboard |
| `aspect_ratio` | auto / 16:9 / 9:16 / 4:5 / 5:4 / 1:1 | `auto` follows the input image |
| `motion_prompt` | str | Optional natural-language gesture direction |
| `expressiveness` | low / medium / high | Default `low` |
| `output_directory` | str | Optional folder to also save the video into (supports `{project_dir}/...`) |

Outputs: `video` (VideoUrlArtifact, saved to the project's static files), `video_id` (str), plus `was_successful` / `result_details` and **Succeeded** / **Failed** control paths.

Generation typically takes 2–5 minutes for short clips; the node polls up to 30 minutes.

### HeyGen Video Translate

Video → one or more translated videos with lip-sync and the original voice preserved.

| Parameter | Type | Notes |
|---|---|---|
| `video` | VideoUrlArtifact | Connect the Avatar Video output directly |
| `target_languages` | list of str | HeyGen language names, e.g. `Spanish (Spain)`, `Catalan (Spain)` |
| `title` | str | Optional |
| `mode` | speed / precision | `precision` gives higher lip-sync quality |
| `output_directory` | str | Optional folder to also save all translated videos into (supports `{project_dir}/...`); files are named `<title>_<language>.mp4` |

Outputs: `videos` (list of VideoUrlArtifact, in target-language order), `language_map` (language → URL), plus status outputs as above.

All languages are submitted in a single API call and polled independently — **if one language fails, the others still complete** and the failures are listed in `result_details`. Requested languages are validated against HeyGen's live language list at submit time, with suggestions for near-misses.

## Notes & limits

- Uploads to HeyGen (image, audio, and locally-hosted source videos for translation) are capped at **32 MB** by HeyGen's asset API.
- Generated videos are downloaded and saved to Griptape's static files, because HeyGen's result URLs are presigned and expire.
- The nodes use HeyGen **API v3** (`/v3/assets`, `/v3/videos`, `/v3/video-translations`) with idempotency keys on every submission and `Retry-After`-aware rate-limit handling.
- No secrets live in this repo; the API key is read from Griptape's SecretsManager at runtime.

## Development

```bash
uv sync
uv run ruff check heygen
```

Library manifest: [heygen/griptape_nodes_library.json](heygen/griptape_nodes_library.json). See [SPEC.md](SPEC.md) for the full design and verification notes.
