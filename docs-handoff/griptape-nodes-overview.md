---
title: HyperReal Nodes for Griptape
section: Pipeline
subsection: Griptape Nodes
category: Overview
excerpt: One custom node library carrying all of HyperReal's service integrations and video tooling.
tags: [pipeline, ai, video, workflow, setup, automation]
updated: 2026-09-18
---

> lede: What the HyperReal node library is, how to install and configure it, and the conventions every node in it shares.

Most of HyperReal's production pipeline runs through hosted services — HeyGen for lipsync, Topaz for upscaling, WaveSpeed for talking-head generation, Tavus for conversational replicas, RunComfy for ComfyUI workloads — plus a growing set of local video operations (tracking, cropping, compositing, matting, chunking) that would otherwise mean a round trip through an NLE for every iteration. The `griptape-nodes-library-hyperreal` repository packages all of it as a single [Griptape Nodes](https://www.griptapenodes.com/) library, so a workflow canvas can take a plate from ingest to delivery without leaving the engine. As of version 0.18.1 the library registers 28 nodes.

## 1. Installation

The library installs like any Griptape Nodes library: clone the repository, then in the Griptape Nodes editor go to **Settings → Libraries → + Add Library** and point it at `hyperreal/griptape_nodes_library.json` inside the clone. Absolute paths work; the repo does not need to live in the engine's workspace directory. There is also a one-click install link in the repository README that registers the library straight from GitHub.

The library requires engine version 0.86.0 or newer — the nodes depend on `SuccessFailureNode` and the project/macro path system introduced there. Python dependencies (`requests`, `boto3`, `opencv-python`, `numpy`, `static-ffmpeg`, `rembg`, `pillow`) are declared in the manifest and installed by the engine automatically.

> **Warning:** "Refresh Libraries" re-reads the manifest but does **not** reload changed node Python, and secrets added while the engine is running are invisible until the next start. After pulling a library update or adding a secret, restart the engine — and if you drive the engine over MCP, reconnect the client afterwards.

## 2. Secrets

Every service integration authenticates through a named secret set under **Settings → API Keys & Secrets**. Nodes validate their secret before running and fail with a readable message naming the missing key, so an unconfigured secret surfaces on the canvas rather than as a stack trace. Only the secrets for the nodes you actually use need to be set.

| Secret | Used by |
|--------|---------|
| `HEYGEN_API_KEY` | HeyGen Avatar Video, HeyGen Video Translate |
| `TOPAZ_API_KEY` | Topaz Video Upscale, Topaz Image Upscale |
| `WAVESPEED_API_KEY` | WaveSpeed Image Edit, InfiniteTalk, InfiniteTalk V2V |
| `DO_SPACES_KEY` / `DO_SPACES_SECRET` / `DO_SPACES_REGION` / `DO_SPACES_ENDPOINT` | Upload to Spaces (region or endpoint suffices; the other is derived) |
| `RUNCOMFY_TOKEN` | RunComfy SCAIL Infinite |
| `TAVUS_API_KEY` | Tavus Train Replica, Tavus Replica Status |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Send Email |

> **Note:** The Spaces endpoint must not include the bucket name — `https://atl1.digitaloceanspaces.com`, not `https://mybucket.atl1.digitaloceanspaces.com`. With the bucket in the endpoint, uploads appear to succeed but land at unreachable URLs.

## 3. Conventions Every Node Shares

Knowing the house conventions makes an unfamiliar node predictable. Every action node extends `SuccessFailureNode`: alongside its data outputs it exposes **Succeeded** and **Failed** control paths plus `was_successful` (bool) and `result_details` (str). Errors are translated into operator-readable sentences — a HeyGen failure surfaces HeyGen's own failure message, an SMTP 535 explains the app-password requirement — never a bare traceback. The Failed control path means a workflow can route around a failure (for example, mailing the error) instead of simply halting.

Configuration that a whole shot shares lives in dedicated settings nodes — **Shot Settings** for plate work, **Tavus Training Settings** for replica training. These are data nodes whose every property is also an output, wired once into each consumer, so changing a resolution or a bucket name in one place propagates to the whole canvas. Rendered media is written through the engine's static-files store, which means outputs get stable URLs the editor can preview, and video encodes are tagged BT.709 so colors match in QuickTime and Resolve.

Finally, hosted services can only fetch inputs over public HTTP. That is why **Upload to Spaces** sits in front of nearly every API node: it publishes the artifact to a DigitalOcean Spaces bucket and hands the public URL downstream. Nodes that need a URL refuse `localhost` addresses outright rather than letting the remote service fail obscurely.

## 4. The Node Map

The library is organized into editor categories; each group has its own documentation page in this section.

| Category | Nodes | Documented in |
|----------|-------|---------------|
| HeyGen / WaveSpeed | Avatar Video, Video Translate, InfiniteTalk, InfiniteTalk V2V, Image Edit | Generation Services |
| Topaz | Video Upscale, Image Upscale | Upscaling |
| Face Prep | Detect Head Region, Crop To Region, Composite Region Back, Zoom To Head, Composite Face Back | Face Prep |
| Figure Prep / ComfyUI | Detect Figure Track, Reposition Tracked Crop, RunComfy SCAIL Infinite | Figure Prep |
| Composite / Matte | Composite Over Background, Composite Bottom Band, Overlay Zoomed Video, Extract Image Matte, Refine Video Matte | Compositing & Mattes |
| Config / Storage / Chunk / Notify | Shot Settings, Upload to Spaces, Split Video Frame Accurate, Join Video Chunks, Send Email | Utilities |
| Tavus | Training Settings, Train Replica, Replica Status | Tavus Replica Training |

Reference workflows built from these nodes live in the separate `hr-griptape-workflows` repository and are documented in the workflow pages of this section.
