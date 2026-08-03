# SPEC: HeyGen Node Library for Griptape Nodes

**Repo:** `production-hr/griptape-nodes-library-heygen`
**Local:** `X:\Dev\hr-griptape-nodes\griptape-nodes-library-heygen`
**Status:** Draft for implementation. Items marked ⚠️ must be verified against live docs before coding.

---

## 0. Scope of this spec

Covers the **HeyGen** node library only — the first custom library HyperReal builds. A shared **DigitalOcean Spaces** utility library is specified in §6 because it's needed by later libraries (ViewComfy), not by HeyGen itself.

**Target pipeline:**

```
approved image ─┐
                ├─► HeyGen Avatar Video ─► English lipsync video ─► HeyGen Video Translate ─► [ES, CA, ...]
approved audio ─┘
```

Both inputs are pre-approved before the video stage. Batch size is 1–5. No queueing or retry infrastructure.

---

## 1. Repos and publishing

**GitHub org:** `https://github.com/production-hr`
**Maintainer:** `tim-hyperreal`

**One repo per library**, following the ecosystem convention. This matters: the "Add to Griptape Nodes" deep link is `https://nodes.griptape.ai/#library-management?git=<repo-url>`, and every published library in the directory has its manifest at the repo root. ⚠️ Whether the installer resolves a manifest in a subdirectory is unverified — don't find out the hard way.

| Repo | Purpose |
|---|---|
| `production-hr/griptape-nodes-library-heygen` | HeyGen nodes (this spec) |
| `production-hr/griptape-nodes-library-dospaces` | DigitalOcean Spaces upload (§6) |

**Local layout** — `X:\Dev\hr-griptape-nodes` is a working parent folder holding clones, not itself a repo:

```
X:\Dev\hr-griptape-nodes\
├─ griptape-nodes-library-heygen\      → production-hr/griptape-nodes-library-heygen
│  ├─ SPEC.md                          ← this document
│  ├─ README.md                        ← must include the Add to Griptape Nodes link
│  ├─ griptape-nodes-library.json      ← manifest, at repo root
│  ├─ pyproject.toml
│  └─ nodes\
│     ├─ __init__.py
│     ├─ _client.py
│     ├─ avatar_video.py
│     └─ video_translate.py
└─ griptape-nodes-library-dospaces\    → production-hr/griptape-nodes-library-dospaces
```

Scaffold each from the official template rather than an empty folder:

```
git clone https://github.com/griptape-ai/griptape-nodes-library-template.git
```

**Registration:** point the engine at the absolute path to `griptape-nodes-library.json` (Settings → App Events → Libraries to Register). Registration by absolute path means these repos do **not** need to live inside the Griptape workspace directory — which is deliberate, since the workspace holds `.env` with our secrets and must stay out of git.

⚠️ **Decide repo visibility before first push.** Public repos make the deep-link install work for anyone on the team with no extra setup. Private repos require the engine machine to have git credentials for `production-hr`. Neither is wrong; pick one knowingly. Nothing in these repos should be secret regardless — if a repo can't be public for non-secret reasons, that's fine, but no key should ever be the reason.

⚠️ Confirm the exact manifest schema from the template and from the **Authoring Libraries** doc page before hand-writing `griptape-nodes-library.json`.

---

## 2. Griptape interfaces to build against

Verified from the current docs and library template.

**Imports:**
```python
from griptape_nodes.exe_types.node_types import ControlNode, DataNode, AsyncResult
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterList
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
```

**Base class:** every node here is a long-running remote call that can fail meaningfully. ⚠️ Evaluate `SuccessFailureNode` — the docs describe it as intended for operations that may fail gracefully, which fits better than plain `ControlNode`. Confirm its interface in the comprehensive guide, then pick one and use it consistently.

**Async pattern (mandatory — never block the graph):**
```python
def process(self) -> AsyncResult[None]:
    yield lambda: self._process()

def _process(self) -> None:
    result = self._long_running_call()
    self.parameter_output_values["output_name"] = result
```

**Secrets:**
```python
api_key = GriptapeNodes.SecretsManager().get_secret("HEYGEN_API_KEY")
```
No hardcoded keys, no `.env` reads inside node code.

**Constructor conventions:** call `super().__init__(**kwargs)` first; set `self.category` and `self.description`.

**Artifact types:** `ImageUrlArtifact` / `ImageArtifact` for images, `VideoUrlArtifact` (`griptape.artifacts.video_url_artifact`) for video output. Use `ProjectFileParameter` when writing an output file to the project.

**Lists:** `ParameterList` for multi-value inputs; read with `self.get_parameter_list_value(name)`, which always returns a list.

---

## 3. HeyGen API contract

**Base:** `https://api.heygen.com`
**Auth:** `X-Api-Key: <key>` header.
**Version:** build against **v3**. v1/v2 are supported only until **2026-10-31** — do not build new nodes on them.

**Status polling:** `GET /v3/videos/{video_id}` returns status, `video_url`, `thumbnail_url`, `duration`, and failure info. On failure, surface `failure_code` and `failure_message` in the node error — do not raise a bare HTTP error.

**Idempotency:** the API accepts a client-supplied idempotency key. Keys are 1–255 chars from `[A-Za-z0-9_:.-]`; a UUID is a safe default. Scope is per-endpoint and per-resource. Repeat calls within 24h replay the original response; a retry while the original is in flight returns `409 request_in_progress`. Generate one per submission and store it on the node for the duration of the run.

**Rate limits:** respect `Retry-After` and back off.

**Assets:** upload images and audio via the Upload Asset API, which returns a key/ID referenced in the generation call. **HeyGen inputs do not require public URLs** — we upload bytes directly. DigitalOcean Spaces is not needed on the HeyGen input path.

⚠️ Verify the exact Upload Asset endpoint path, request format (raw bytes vs multipart), and the response field names for image and audio before implementing `_client.py`.

---

## 4. Node: `HeyGen Avatar Video`

Image + audio → English lipsync video. Uses the Avatar IV engine (server default when `engine` is omitted).

**Inputs**

| Parameter | Type | Mode | Notes |
|---|---|---|---|
| `image` | `ImageArtifact`, `ImageUrlArtifact` | INPUT | The approved AI-generated image |
| `audio` | audio artifact | INPUT | The approved audio file. ⚠️ Confirm Griptape's audio artifact type name from the ElevenLabs library |
| `video_title` | `str` | PROPERTY | Required by the API |
| `orientation` | `str` (enum) | PROPERTY | portrait / landscape |
| `expressiveness` | `str` (enum) | PROPERTY | low / normal / high. Photo avatars only; defaults to `low` |
| `custom_motion_prompt` | `str` | PROPERTY | Optional; natural-language gesture control |
| `enhance_custom_motion_prompt` | `bool` | PROPERTY | Lets HeyGen refine the motion prompt |

**Outputs**

| Parameter | Type | Notes |
|---|---|---|
| `video` | `VideoUrlArtifact` | The generated video |
| `video_id` | `str` | Retained for translation and debugging |

**Process**

1. Read `HEYGEN_API_KEY` from SecretsManager.
2. Upload image → `image_key`. Upload audio → audio asset ID.
3. `POST /v3/videos` with `type: "avatar"`, the image reference, and the audio asset ID.
   **`audio` and `script` are mutually exclusive** — we always use audio, never script. Do not expose a `script` parameter on this node; a separate node can be added later if script-driven generation is ever needed.
4. Poll `GET /v3/videos/{video_id}` until terminal. Short clips typically finish in 2–5 minutes; longer or high-res runs can take 10–20. Use a 5s initial interval backing off to 15s, with a generous overall timeout.
5. On success set `video` and `video_id`. On failure surface `failure_code` / `failure_message`.

⚠️ Verify the exact field names for direct image input on `POST /v3/videos` — the reference mentions photo avatars, digital twins, and direct image input, and the payload shape differs between them.

---

## 5. Node: `HeyGen Video Translate`

English video → one or more translated videos, with lip-sync and the original voice preserved.

**Inputs**

| Parameter | Type | Mode | Notes |
|---|---|---|---|
| `video` | `VideoUrlArtifact` | INPUT | Accepts the upstream node's output directly |
| `target_languages` | `ParameterList[str]` | INPUT/PROPERTY | e.g. `["Spanish", "Catalan"]` |
| `title` | `str` | PROPERTY | Optional |

**Outputs**

| Parameter | Type | Notes |
|---|---|---|
| `videos` | `list[VideoUrlArtifact]` | One per target language, order matching input |
| `language_map` | `dict` | language → video URL, for downstream routing |

**Process**

1. Read the language list via `get_parameter_list_value("target_languages")`.
2. Submit a translation job per language. ⚠️ **Check whether the endpoint accepts multiple target languages in a single call.** Sources conflict. Design the node's public interface around a list either way — if the API takes one language per call, fan out internally. The graph shouldn't care.
3. Poll each job to completion. Run them concurrently, not serially; with 1–5 jobs this is a small thread pool or `asyncio.gather` inside the `AsyncResult` lambda.
4. Emit both outputs.

**Failure policy:** if some languages succeed and others fail, do **not** fail the whole node. Emit successful results and report which languages failed and why. Regenerating five languages because Catalan hiccuped is exactly the frustration we're leaving Airtable to avoid.

⚠️ Verify **Catalan** is in HeyGen's supported language list before promising it. 175+ languages are claimed, but confirm this specific one and the exact identifier format (display name vs ISO code).

---

## 6. Companion library: DigitalOcean Spaces

Not required by HeyGen, but required by ViewComfy later and useful for archiving deliverables. Ships as its own repo — `production-hr/griptape-nodes-library-dospaces` — so it registers and versions independently.

DO Spaces is S3-compatible — use `boto3` with a custom endpoint URL rather than any DO-specific SDK.

**Node: `Upload to Spaces`**

- Inputs: artifact (image / video / audio), `bucket`, `key_prefix`, `public` (bool)
- Output: `url` (str) — the public URL
- Secrets: `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `DO_SPACES_REGION`, `DO_SPACES_ENDPOINT`

This node is what turns any locally-generated asset into something an external API can fetch — the piece the local-engine architecture otherwise lacks.

---

## 7. Definition of done

- [ ] Library registers cleanly and both nodes appear in the editor's Libraries panel
- [ ] Image + audio produces an English lipsync video end to end
- [ ] That video feeds directly into translate and produces Spanish + Catalan
- [ ] A deliberately bad API key produces a readable node error, not a stack trace
- [ ] A failing single language doesn't destroy the other results
- [ ] No secrets anywhere in the repo; `.env` is gitignored
- [ ] README includes the "Add to Griptape Nodes" install link for the team

---

## 8. Reference

- Node development guide: `github.com/griptape-ai/griptape-nodes-node-development-guide` (`node-development-guide-v3.md`)
- Library template: `github.com/griptape-ai/griptape-nodes-library-template`
- Custom node docs (**note: reorganized — these are the current paths**), all under `docs.griptapenodes.com/en/stable/development/custom_nodes/`:
  - `getting_started/` — start here
  - `parameters/` and `parameter_ui_reference/` — parameter definition and UI
  - `execution_and_lifecycle/` — `process()`, async, node lifecycle
  - `error_handling/` — best practices and error handling
  - `authoring_libraries/` — **the library manifest schema**
  - `examples/` and `example_control_node.py` — working patterns
- Machine-readable docs: `docs.griptapenodes.com/en/stable/llms.txt` and `llms-full.txt`
- HeyGen API: `docs.heygen.com` (has an `llms.txt`)
- Closest existing patterns to copy: the Kling, Luma, and Minimax libraries — all async video-generation APIs with the same submit/poll shape.
