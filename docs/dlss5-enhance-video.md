# DLSS 5 Enhance Video

Reconstructs skin, hair and fabric micro-detail in a video using NVIDIA DLSS 5 neural rendering.
It is the detail pass that AI generation is worst at: pores, brow structure, separated beard hair,
stubble, sweat — material response rather than sharpening.

**Category:** `video/dlss5` · **Class:** `Dlss5EnhanceVideo` · **File:** `dlss5/enhance_video.py`

## Why this one runs locally

Every other ComfyUI node in this library targets RunComfy. This one cannot. DLSS 5 drives the GPU
through a native worker (`nvngx_dlssnr.dll` under a ReShade/RenoDX carrier), so it has to execute on
a machine with the runtime installed and an RTX card in it. There is no hosted equivalent to call.

The node talks to a ComfyUI on `127.0.0.1`. It probes port **8000** first (ComfyUI Desktop's own
port, so an already-open Desktop is reused) and then **8188**. If neither answers and
`auto_start_server` is on, it launches a headless ComfyUI on 8188 and waits for it.

> That launched process **outlives the node and the workflow**. It is deliberate — the next run
> reuses it instead of paying the ~40 s startup again — but nothing shuts it down for you. Close it
> from Task Manager, or turn `auto_start_server` off and manage ComfyUI yourself.

## Requirements

- Windows, RTX 30/40/50-series. 40-series runs the community runtime via RenoDX; RTX 20 and older
  are unsupported. A rejected GPU/driver combination shows up as `0xBAD00001`.
- ComfyUI with `Blueforcer/ComfyUI-DLSS5-Enhancer` installed and `install_runtime.py` run. The node
  checks `/object_info` and fails with a readable message if the node pack is missing.

## Defaults are not the node pack's defaults

Two are deliberately different, from measured results on our footage:

| parameter | node pack | here | why |
|---|---|---|---|
| `local_tone_strength` | 1.0 | **0.0** | At 1.0 it applies a local tone map that crushes highlights and desaturates ~18%. That is the entire colour shift people attribute to the neural pass. At 0.0 the pass is colour neutral. |
| `quality` | Auto | **Good** | `Max` is constant-quality and costs ~8 MB per second of footage. Use `Max` only when measuring. |

`dlss_model_preset` stays at **M**, which retains the most texture — the ordering Default < J < K <
L < M is monotonic and M is the top.

## Two things decide whether this helps, and neither is a setting

**Motion decides the sign.** Static and slow shots gain detail. Fast-motion shots lose it — on fast
motion the optical-flow estimate misregisters and the accumulated history smears instead of
reinforcing. Measured on boxing footage:

| shot | `motion=auto` | `motion=none` |
|---|---|---|
| standing, static | **1.29x** | 1.13x |
| standing, static | **1.10x** | 0.87x |
| fast punch | 0.73x | **0.89x** |
| fast punch | 0.62x | **0.85x** |

On fast motion, `motion=none` limits the damage but never produces a gain. The honest answer there
is to skip the pass.

**Resolution decides the size.** At HD the result is roughly parity. At 4K it returns ~2.15x the
detail for ~1.46x the shimmer — measured at 2.15x and 2.17x on two unrelated clips, the most
reproducible figure in the whole evaluation.

So **put this node after your upscale, not before it**, and before frame interpolation — frame rate
barely affects the result (2.08x at 25 fps against 2.15x at 60 fps) and running at the lower rate
costs 2.4x less.

    generate HD -> upscale 4K -> DLSS 5 -> interpolate -> grade / stabilise -> deliver

Also: **never chain this after a Deblur IC-LoRA pass.** On already-deblurred footage DLSS 5 removes
53% of the detail that pass added. They undo each other; pick one.

## Parameters

### Inputs

| name | type | default | notes |
|---|---|---|---|
| `video` | VideoUrlArtifact \| str | — | Artifact, `file://` URL, http(s) URL or a plain local path. A local path is passed to ComfyUI by reference and never moves through memory. |
| `dlss_model_preset` | str | `M` | Default, J, K, L, M. |
| `local_tone_strength` | float | `0.0` | Leave it. See above. |
| `motion` | str | `auto` | `auto`, `optical_flow` (identical to auto), `none`. |
| `upscaling_mode` | str | `1x (DLAA / native)` | The other modes do nothing on 40-series — the signed runtime rejects the low-resolution colour contract and falls back to native. |
| `output_directory` | str | `""` | Absolute path, or empty for ComfyUI's output folder. |

### Encoding *(collapsed)*

`codec` (H.264), `container` (MP4), `quality` (Good), `copy_audio` (true).

### Advanced *(collapsed)*

`local_structure_strength` (1.5 — moving it to 2.0 changed detail by 0.02x, so it is not a lever),
`skin_structure_strength` (2.0), `automatic_mask` (true — the gate for skin structure),
`nr_style`, `nr_preset` (measured identical at every value), `nr_intensity` (above 1.0 does
nothing), `scene_change_threshold` (no measurable difference between 0.24 and 0.90), `max_frames`,
`server_url`, `auto_start_server`, `comfy_python`, `comfy_main`, `timeout_seconds`.

### Outputs

| name | type | notes |
|---|---|---|
| `video_out` | VideoUrlArtifact | Saved through `StaticFilesManager`, ready for downstream nodes. |
| `output_path` | str | Absolute path of the file DLSS 5 wrote, for a local next step. |
| `frames` | int | Frames processed. |
| `status` | str | `completed`, or `<kind>: <detail>` on failure. |
| `was_successful` | bool | |

## Throughput

Roughly 17 fps at 1088x1920 on an RTX 4080 SUPER — faster than realtime. A 5 s 4K clip takes about
22 s at 25 fps, 45 s at 60 fps.

## Cancelling

The poll loop is cancel-aware: stopping the workflow in the editor sends `/interrupt` to ComfyUI and
the node reports `cancelled`. It does not stop an auto-started server.
