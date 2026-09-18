---
title: Griptape Nodes Utilities
section: Pipeline
subsection: Griptape Nodes
category: Utilities
excerpt: Shared shot settings, Spaces uploads, email notification, and frame-exact video chunking.
tags: [pipeline, video, workflow, automation, setup, reference]
updated: 2026-09-18
---
> lede: Five utility nodes carry the plumbing under every HyperReal workflow: one settings source, public hosting for media, job notifications, and frame-exact chunking with rejoin.

Every generation workflow in the HyperReal library depends on the same unglamorous services: values that several nodes must agree on, a public URL that hosted APIs can fetch media from, an email when an hours-long job finishes, and a way to cut a clip that is too long for a single processing pass into pieces without losing or duplicating a frame. These five nodes exist so that none of that is improvised per workflow. Four of them (all except Shot Settings) are standard success/failure nodes: each exposes the usual execution status outputs — a success and failure path plus `was_successful` and `result_details` — which are not repeated in the parameter tables below.

## 1. Shot Settings

Shot Settings is one node holding the values that several nodes in a shot must agree on. The motivating case is the two-pass lipsync: both passes are only correct when they share an aspect ratio, a resolution, and an expressiveness level. Set on each node individually, those are three chances to diverge — and a divergence does not announce itself, it shows up as a broken composite after both generations have been paid for. Wiring every consumer from this one node makes agreement structural instead of remembered.

It is a DataNode, not an executable node: it has no control wiring, no success/failure outputs, and nothing to run in order. It resolves automatically as a dependency of whatever reads it, and its `process` step does nothing but publish each property as an output. Note that `auto` is deliberately absent from the aspect ratio choices. HeyGen documents `auto` as following the input image; in practice it returned 16:9 for a portrait input, which pillarboxes the subject and silently poisons a downstream overlay. A shot's aspect is a decision, so this node makes you make one.

| Parameter | Default | Purpose |
|---|---|---|
| `aspect_ratio` | `9:16` | Aspect for every generated clip in the shot. Choices: 9:16, 16:9, 4:5, 5:4, 1:1. Wire to the lipsync nodes. |
| `resolution` | `1080p` | Render resolution for every generated clip. Choices: 1080p, 720p. |
| `expressiveness` | `low` | Motion level for every lipsync pass. Choices: low, medium, high. Keep it low for a two-pass overlay — camera drift differs between generations and makes the zoomed face slide against the base. |
| `upscale_long_edge` | `1920` | Target long edge for the still upscale before the zoomed lipsync pass. Wire to Topaz Image Upscale. |
| `output_directory` | empty | Folder every node in the shot saves a copy into, e.g. `{project_dir}/outputs`. Blank means each node keeps its own setting. |

Every parameter doubles as an output of the same name — that is the whole point of the node.

## 2. Upload to Spaces

Hosted APIs fetch media from URLs, and the engine's local static-file URLs are not reachable from the outside. Upload to Spaces closes that gap: it takes an image, audio, or video artifact, uploads it to a DigitalOcean Spaces bucket, and returns the public URL. Tavus replica training is the canonical consumer — Tavus fetches the training video from a public URL — and ViewComfy has the same requirement. Spaces is S3-compatible, so the node is boto3 with a custom endpoint; there is no DigitalOcean-specific SDK involved.

Credentials come from four secrets under Settings > API Keys & Secrets: `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `DO_SPACES_REGION`, and `DO_SPACES_ENDPOINT`. Key and secret are always required; region and endpoint are derivable from each other, so only one of the two is needed. The node validates all of this before the workflow runs and names exactly what is missing. The content type of the upload is sniffed from the file's magic bytes (PNG, JPEG, WAV, MP3, MP4) rather than trusted from artifact metadata, so browsers and APIs treat the object correctly. Failures are mapped to readable errors that name the setting to fix: a rejected access key points at `DO_SPACES_KEY`, a bad signature at `DO_SPACES_SECRET`, a missing bucket at the bucket name and region/endpoint, an unreachable endpoint at the endpoint and your network.

> **Note:** Newly added secrets only load at engine startup. After adding or changing any of the four `DO_SPACES_*` secrets, restart the engine.

| Parameter | Default | Purpose |
|---|---|---|
| `artifact` | — | The media asset to upload (image, audio, or video artifact; raw bytes, `http(s)` URLs, `data:` URIs, workspace paths, and `{project_dir}` macros all work). |
| `bucket` | empty | Spaces bucket name. Required. |
| `key_prefix` | empty | Folder-style prefix for the object key, e.g. `gaudi/welcome/`. A trailing slash is added if missing. |
| `filename` | empty | Optional filename override. Left empty, the name comes from the artifact, falling back to a generated name with a content-sniffed extension. |
| `public` | `true` | Uploads with a `public-read` ACL. Required for ViewComfy and browser access; off makes the object private. |
| `allow_empty` | `false` | If no artifact is connected or loaded, succeed with an empty `url` instead of failing. |

`allow_empty` exists for optional inputs that stay wired on the canvas. The Tavus consent video is the model case: a Human replica requires one, an AI replica ignores it, and the consent Load Video stays in the workflow either way — with `allow_empty` on, an empty run simply skips the upload and reports success with an empty URL.

Outputs are `url`, the public address of the object in the form `https://<bucket>.<endpoint-host>/<key>`, and `key`, the object key kept for downstream deletes or replacements.

## 3. Send Email

Tavus training runs for hours; nobody watches a canvas that long. Send Email is a plain-text SMTP notification node — "training launched", "render finished" — built on the Python standard library with no third-party dependencies. It is for pipeline notifications, not bulk mail. The usual pattern is to wire another node's summary output straight into `body`, and the reference workflows do exactly that: `tavus_launch` ends in a "launched" email, `tavus_check` in a "completed / failed" one.

Configuration is four secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`. The port selects the transport: 465 opens an SSL connection, anything else (587 being the standard choice) connects plain and upgrades with STARTTLS. For Google Workspace the values are `smtp.gmail.com`, `587`, the sending address, and a 16-character app password — a normal account password will not work, and app passwords require 2-Step Verification to be enabled on the account. Google displays app passwords as four spaced groups; the spaces are display-only and the node strips all whitespace from the stored password before use, so pasting either form works. An authentication failure produces a diagnostic that states the user, host, port, and cleaned password length, and spells out the app-password requirements, instead of dumping a raw SMTP trace. The SMTP connection times out after 30 seconds, and as with every secret, the engine must be restarted after adding or changing any of the four.

| Parameter | Default | Purpose |
|---|---|---|
| `to` | empty | Recipient address(es), comma-separated. Required. |
| `subject` | empty | Email subject; an empty subject is sent as `(no subject)`. |
| `body` | empty | Plain-text body. Wire a node's summary output here. |
| `from_address` | empty | Sender address. Empty means `SMTP_USER`. |
| `enabled` | `true` | Off skips sending entirely and still reports success — for dry runs. |

The one data output is `sent`, a boolean that is true only when the SMTP server accepted the message (false on failure and on a disabled dry run).

## 4. Split Video Frame Accurate

Segmentation and other GPU-bound processing has a memory ceiling: past some frame count, a single pass runs out of VRAM. The answer is to chunk the clip — but the obvious way to chunk video is wrong. Time-based splitting (ffmpeg `-ss` with a stream copy, which is also what the stock Split Video node does) snaps to keyframes: chunks silently overlap, overshoot their requested range, or drop the tail frame. Frame counts can hide it, because two errors can cancel — and for matte work it is fatal, since one duplicated or missing frame desynchronises the matte from the plate for the rest of the timeline.

This node cuts on exact frame boundaries instead. It first decodes the source to get an authoritative frame count (container metadata is not trusted), then cuts every chunk in a single decode pass using an explicit frame-range select filter, re-encoding each chunk with x264. Re-encoding is unavoidable — frame-exact cutting cannot be done with a stream copy — which is why the CRF is exposed. Then it verifies: every chunk is decoded back, and if any chunk's frame count does not equal its requested range, or contiguous chunks fail to cover the source exactly, the node fails loudly rather than handing on a bad chunk. Audio is dropped from the chunks; this node exists to feed frame-accurate processing, not editorial. ffmpeg itself is fetched automatically on first use, and the split step has a one-hour timeout.

| Parameter | Default | Purpose |
|---|---|---|
| `video` | — | The video to split. |
| `mode` | `auto chunks` | `auto chunks` divides the clip by `max_frames_per_chunk`; `explicit ranges` uses the `frame_ranges` field. |
| `max_frames_per_chunk` | `300` | Auto mode: largest chunk to emit. Size it from VRAM — the node's guidance for SAM3 is roughly a fixed base plus ~8 MB/frame at 1080p-class and ~12.7 MB/frame at 4K. |
| `overlap_frames` | `0` | Auto mode: frames each chunk repeats from the previous one, for blending seams. 0 tiles the source exactly with no duplicates. Must be smaller than `max_frames_per_chunk`. |
| `frame_ranges` | empty | Explicit mode: one inclusive, 0-based `start-end` per line, e.g. `0-304`, optionally `0-304|Label`. Ranges past the last frame are rejected. |
| `quality_crf` | `14` | x264 CRF for the chunks; 14 is visually lossless, lower is bigger. |
| `output_directory` | empty | Optional folder to also write the chunks into, e.g. `{project_dir}/outputs`. Empty skips file copies. |

Outputs: `chunks`, the frame-exact chunks in order as a list of video artifacts; `frame_ranges_used`, the inclusive ranges actually cut, one per line, for the reassembly step or for provenance; `num_chunks`; and `total_frames`, the decoded frame count of the source — wire this into the joiner's `expected_frames` to prove the round trip.

> **Warning:** With `overlap_frames` greater than 0, the chunks intentionally contain duplicated frames. Join them directly and the result carries those duplicates — which the joiner's `expected_frames` check will catch when wired to the splitter's `total_frames`. Overlap is for processes that blend seams and discard the overlap before reassembly.

## 5. Join Video Chunks

The companion to the splitter, and the reason a chunked loop can be wired at all. It exists instead of the stock Concatenate Videos node for two reasons. First, a ForEach End outputs an untyped `list`, which the stock node's typed `list[VideoUrlArtifact]` input refuses — this node's `videos` input accepts the untyped list, so processed chunks flow straight out of the loop. Second, splitting is only frame-exact if the join is too: this node decodes the result and checks it against the sum of its parts, failing loudly on any drift.

The concatenation itself is a stream copy when the chunks share codec parameters — the usual case when they all came from one splitter — and falls back to a re-encode (x264 CRF 14) when they do not; the status details report which path was taken. After joining, the clip's decoded frame count must equal the sum of the per-chunk counts, and, when `expected_frames` is set, that number as well. Like the splitter, the join is video-only — audio is dropped on both paths — and the operation has a one-hour timeout.

| Parameter | Default | Purpose |
|---|---|---|
| `videos` | — | The chunks to join, in order — e.g. straight from a ForEach End `results` output. |
| `expected_frames` | `0` | If greater than 0, fail unless the joined clip decodes to exactly this many frames. Wire the splitter's `total_frames` here. |
| `output_directory` | empty | Optional folder to also write the joined clip into, e.g. `{project_dir}/outputs`. |

Outputs: `video`, the joined clip, and `total_frames`, its decoded frame count. A split, per-chunk processing loop, and join wired this way is provably lossless in frame terms: if any stage drops or duplicates a frame, one of the two verification gates fails and names the numbers instead of shipping a subtly wrong render.
