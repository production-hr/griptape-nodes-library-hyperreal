# SPEC: DigitalOcean Spaces Node Library for Griptape Nodes

**Repo:** `production-hr/griptape-nodes-library-dospaces`
**Local:** `X:\Dev\hr-griptape-nodes\griptape-nodes-library-dospaces`
**Status:** Draft for implementation.

The HeyGen library this spec originally covered **shipped on 2026-08-03** — its as-built documentation, API contract, implementation notes, and gotchas now live in [README.md](README.md). This document specifies the next library: the shared **DigitalOcean Spaces** upload utility, needed by ViewComfy (whose APIs require publicly fetchable input URLs, unlike HeyGen's direct-upload asset API) and useful for archiving deliverables.

---

## 0. Scope

One node: upload a media artifact to a DO Spaces bucket and return its public URL. This is the piece that turns any locally-generated asset into something an external API can fetch — the gap the local-engine architecture otherwise has. Ships as its own repo so it registers and versions independently.

## 1. Conventions (verified by the HeyGen build — follow, don't re-derive)

- Scaffold from `griptape-ai/griptape-nodes-library-template`, then restructure to the current ecosystem layout:
  - Manifest at `dospaces/griptape_nodes_library.json` (**underscored filename, library-named subdirectory**; the template's root-level hyphenated manifest is the older style). Node `file_path` entries are manifest-relative.
  - `[tool.hatch.build.targets.wheel] packages = ["dospaces"]` in `pyproject.toml`, or the editable build fails.
- **Self-contained node file** — no shared client module; the engine loads node files individually and cross-file imports are unproven. Copy helpers from the HeyGen nodes rather than importing them.
- **Base class `SuccessFailureNode`** (engine ≥ 0.86.0): `_create_status_parameters()` in the constructor, `_clear_execution_status()` at process start, `_set_status_results(...)` on completion, `_handle_failure_exception(e)` in the except block. Async via `process()` yielding `lambda: self._process()`.
- **Media input handling**: copy `_artifact_to_bytes` / `_resolve_workspace_path` / `_resolve_macro_path` from `heygen/avatar_video.py` verbatim — artifact values arrive as raw bytes, `http(s)` URLs, `data:` URIs, workspace-relative paths, or `{project_dir}` macros, and all forms must work.
- Manifest `metadata.dependencies.pip_dependencies: ["boto3"]`; `engine_version: "0.86.0"`.
- Secrets via `settings[].contents.secrets_to_register`. **Gotcha:** secret registration fires on `app_events.on_app_initialization_complete` — after first install, users must restart the engine (Refresh Libraries is not enough). Say so in the README.
- Repo public on `production-hr`; README carries the `https://nodes.griptape.ai/#library-management?git=<repo-url>` install link.

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

- [ ] Library registers cleanly; node appears in the Libraries panel
- [ ] An image, an audio file, and a video each upload and return a URL that opens in a browser
- [ ] `public=false` produces an object that is *not* publicly fetchable
- [ ] Bad credentials produce a readable node error naming the problem
- [ ] No secrets in the repo; README documents the four secrets and the engine-restart gotcha
- [ ] README includes the "Add to Griptape Nodes" install link

## 4. Reference

- As-built reference implementation: this repo — [README.md](README.md) and `heygen/avatar_video.py` (input handling, SuccessFailureNode wiring, manifest shape)
- DO Spaces S3 compatibility: `docs.digitalocean.com/products/spaces/` (verify the current endpoint URL format for the chosen region before hard-coding examples in the README)
- Custom node docs: `docs.griptapenodes.com/en/stable/development/custom_nodes/` (note: the llms.txt index lists URLs without the required `/en/stable/` prefix)
