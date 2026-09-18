---
title: Compositing and Mattes
section: Pipeline
subsection: Griptape Nodes
category: Compositing & Mattes
excerpt: Local ffmpeg and OpenCV keying, band comps, face overlays, and matte refinement.
tags: [video, pipeline, workflow, reference]
updated: 2026-09-18
---
> lede: Five HyperReal nodes that key, blend, and refine generated footage locally — no round trip through an NLE for the routine comps.

Generated footage rarely arrives finished. A lipsync clip comes back on a green backing and needs a plate behind it; a zoomed face pass needs to land back inside the full-body render; a SAM3 segmentation is temporally rock solid but hard-binary at the edge. The five nodes on this page handle those jobs inside the Griptape graph with ffmpeg, OpenCV, and PIL running on the local machine — no API keys, no per-second billing, and no export-to-Resolve detour for work that is deterministic anyway. Resolve and Nuke stay in the loop for the judgement calls: several of these nodes emit their matte or a preview precisely so a bad edge can be fixed there and fed back in.

Some shared behaviour, stated once. All five are SuccessFailureNodes with the standard execution, success/failure, and `result_details` outputs; `result_details` is worth reading because these nodes report what they measured (sampled key colour, detected placement, matte coverage), not just that they ran. Every node accepts artifacts as URLs, raw bytes, data URIs, workspace-relative paths, or `{project_dir}`-style macros, and every node offers an optional `output_directory` that also writes file copies alongside the normal static-file outputs (a failure to save a copy is reported in `result_details`, never fatal). The video nodes fetch their ffmpeg/ffprobe binaries once on first use via static-ffmpeg.

## 1. Composite Over Background

Composite Over Background puts a subject video over a background still or video, with the alpha coming from one of four `matte_source` modes: `key_auto` samples the key colour from the footage itself, `key_manual` keys on a colour you supply, `external` reads a black-and-white matte video's luma as alpha, and `embedded` uses an alpha channel the foreground already carries (VP9 webm or ProRes 4444). One ffmpeg invocation produces both the composite and a black-and-white `matte` video — wire the matte to a Display Video while tuning, because judging a key from the finished comp is miserable. The matte is also the file you fix in Resolve and feed back as `matte_video` with `matte_source: external`; the node validates that a returned matte matches the foreground's frame count exactly and its frame rate within 0.01 fps, and fails naming both numbers otherwise, because a matte off by a couple of frames is subtly wrong everywhere. Resolution mismatches are fine — the matte is scaled to the foreground.

The `key_auto` sampler reads 32-pixel patches from the four frame corners (inset 5%) across `key_sample_frames` frames and applies three gates before trusting a patch as clean backing: per-channel standard deviation at or under `key_sample_tolerance` (flatness), HSV saturation of at least 60, and HSV value of at least 40 — because a flat black jacket measures flatter than the backing, flatness alone is not enough. Surviving patches then go through an outlier pass that drops anything more than 60 BGR units from the median (a lit gel or coloured prop, not backing). If nothing survives, the node fails with a readable count of what was rejected and tells you to switch to `key_manual`. The measured colour is published on `detected_key_color`; paste it into `key_color` with `key_manual` for a repeatable run. When the corner-to-corner spread exceeds 40 the result notes that the backing is uneven and suggests raising `similarity`.

`similarity` deserves respect: the usable window is narrow and the failure is abrupt. Measured on real generated footage, 0.10–0.12 keeps the subject and clears the backing, 0.15 erased 95% of the subject, and 0.25 erased all of it — green fringe means nudge up toward 0.12; a vanishing subject means you have gone over. Two more failure modes the node catches for you: a video background shorter than the foreground fails outright rather than truncating the performance (a still background is looped instead), and `matte_source: embedded` fails with an explanation when the foreground has no alpha — which is exactly what you get when a provider ignores a webm/alpha request and returns plain mp4. Embedded alpha in webm is detected via the `alpha_mode` tag and decoded with libvpx-vp9, because ffmpeg's native VP9 decoder silently drops the alpha layer. With `output_alpha` on, the node skips compositing entirely and emits a VP9 webm with a real alpha channel, turning it into a standalone keyer; `background` is ignored. Note that `invert_matte` applies only to `external` and `embedded` sources — inverting a key result would just mean keying the subject, which is what changing `key_color` is for.

| Parameter | Default | Purpose |
|---|---|---|
| `foreground_video` | — | The subject video: keyed, matted, or already carrying alpha. |
| `background` | — | Still plate or moving background; ignored when `output_alpha` is on. |
| `matte_source` | `key_auto` | Where alpha comes from: `key_auto`, `key_manual`, `external`, `embedded`. |
| `key_color` | `#00B140` | Key colour for `key_manual`. Mid-saturation digital green; `#00FF00` clips and spills hard on hair. |
| `key_algorithm` | `chromakey` | `chromakey` keys on UV and ignores luma, so lighting drift costs nothing; `colorkey` is RGB, for flat graphics. |
| `similarity` | 0.10 | Key tolerance. Narrow window: 0.10–0.12 works, 0.15 eats the subject. |
| `blend` | 0.10 | Key edge softness. |
| `matte_video` | — | External matte; luma read as alpha, white = opaque. |
| `invert_matte` | false | Flip matte polarity for black-means-opaque sources (`external`/`embedded` only). |
| `despill` | true | Remove green spill from the subject; independent of `matte_source`. |
| `despill_amount` | 0.5 | Despill strength. |
| `matte_erode_px` | 0 | Erode the matte before feathering so the blend edge sits inside the subject. |
| `matte_feather_px` | 0 | Blur the matte edge by this many pixels. Both drop out of the graph at 0. |
| `matte_blend_mode` | `replace` | Reserved; `replace` is the only mode today. |
| `key_sample_frames` | 5 | `key_auto`: frames sampled evenly across the clip. |
| `key_sample_tolerance` | 12.0 | `key_auto`: max per-channel stddev for a corner patch to count as clean backing. |
| `background_fit` | `cover` | How the background conforms (`cover`/`contain`/`stretch`); the foreground is never resampled. |
| `audio_source` | `foreground` | `foreground`, `background`, or `none`. |
| `output_alpha` | false | Emit a VP9 alpha webm instead of compositing; standalone-keyer mode. |
| `crf` | 16 | x264 (or VP9) quality; lower is better and larger. |
| `output_directory` | `""` | Optional folder for file copies. |

Outputs: `composited_video` (mp4, or the alpha webm when `output_alpha` is on), `matte` (the alpha as black-and-white video, white = opaque), and `detected_key_color` (what `key_auto` measured; empty in the other modes).

## 2. Composite Bottom Band

Composite Bottom Band exists for floor treatments — reflections, shadows, floor swaps — where a generative editor has to see the subject it is mirroring. The design inversion that makes it robust: instead of sending the generator a crop (cropped re-renders drift), let it edit the whole frame, then take back only the bottom band of its output. Everything above the seam stays guaranteed-original pixels. The edited frame is Lanczos-resized to the base image's exact dimensions first, so the band is pixel-aligned by construction; if the edit's aspect ratio differs from the base by more than 2% the node notes that the resize stretches it slightly and points at the generator's aspect setting.

The blend matte is opaque everywhere except a linear fade along its top edge — the left, right, and bottom edges coincide with the image borders, where feathering would just fade the edit back out. `band_fraction` outside 0.02–0.9 is rejected, and the feather is clamped to the band height. `color_match` is off by default, deliberately: the band intentionally differs from the original (it contains the new reflection), and a per-channel mean/contrast match would dim exactly the thing you added. Enable it only when the edit shifted the overall floor tone; when on, `result_details` reports the per-channel mean shift it applied. The node is pure PIL — no OpenCV, no ffmpeg.

| Parameter | Default | Purpose |
|---|---|---|
| `base_image` | — | The original, untouched image. |
| `edited_image` | — | The full-frame AI edit; any resolution, resized to match the base. |
| `band_fraction` | 0.25 | Frame-height fraction taken from the bottom; set the seam just above the shoes. Valid 0.02–0.9. |
| `feather_px` | 48 | Height of the linear fade along the top seam, in base-image pixels. |
| `color_match` | false | Per-channel mean/contrast transfer to the original band. Off on purpose; see above. |
| `output_directory` | `""` | Optional folder for a file copy of the composite. |

Outputs: `composited_image`, `matte_preview` (full-frame view of the blend, white = edited content — check the seam height here), and `band_region`, a `hyperreal.bottom_band/1` JSON block giving the band in base-image coordinates for downstream nodes.

## 3. Overlay Zoomed Video

Lipsyncing a full-body frame gives a small, soft face; lipsyncing a zoomed crop gives a good face in the wrong framing. Overlay Zoomed Video closes the loop: it scales the zoomed render back down and blends it over the full-body render, and in the default `align_mode: auto` nothing is placed by hand. The node detects the head in both clips with the vendored YuNet detector (the same ONNX asset the Face Prep nodes use — nothing downloads at run time), taking median face geometry across nine sampled frames per clip. Scale comes from the ratio of the face-box diagonals — measured against a known-scale round trip, the diagonal landed within 0.5% while eye separation was 7.5% out, because a longer baseline absorbs detector jitter — and position lands the overlay's eye midpoint on the base's, since the eyes are where a viewer notices misalignment first. With `refine_alignment` on (the default), a phase-correlation pass over three sampled frames then corrects the last pixel or two; it is gated on a match-confidence threshold of 0.25 and shifts over 40 px are discarded, so when the two renders are too dissimilar to register the node says so and the face-detection estimate stands. `align_mode: manual` bypasses detection entirely and uses `manual_scale` / `manual_center_x` / `manual_center_y`; it is also the documented fallback whenever no face is found or the detected faces are too small to trust. When the zoomed face is less than 1.2x the size of the base's, the result warns that it is barely a zoom and the quality gain will be small.

Two validations protect the render before it starts. First, `frame_tolerance` (default 2): generators round a partial final frame differently run to run, so 1804 vs 1805 frames from the same audio is normal — within tolerance the longer clip's tail simply passes through unblended, and the result notes it. A larger mismatch fails, because both clips must come from the same audio and a real difference would drift the lipsync out of step partway through; frame rates differing by more than 0.01 fps fail too. Second, the node refuses an overlay that is letterboxed or pillarboxed. Those black bars are frame content, so they would be scaled down and blended over the plate as a dark rectangle around the face — and alignment still computes fine, which is what makes it insidious: nothing fails, the render just comes back wrong after both generations have been paid for. The check is deliberately strict so a genuinely dark clip is never mistaken for bars: a bar must be near-zero brightness and near-zero variance, present in every sampled frame, with brighter content between the bars, and paired with a bar on the opposite edge — padding is always centred, so darkness against one edge only (a black chair behind a head) is content and is ignored. The failure message reports the bar widths and the content's real aspect ratio and tells you to re-render the zoomed pass with the aspect set explicitly; wiring both passes from one Shot Settings node prevents the disagreement in the first place.

The blend itself is a feathered `edge_shape` mask (`ellipse` hides a seam best; `rounded_rect` and `rectangle` keep more of the zoomed framing), and `color_match` — on by default — matches the overlay to the base underneath it per frame, in LAB, weighted by the blend mask, because two separate generations always differ slightly in exposure and tone. Frames stream through raw ffmpeg pipes and are re-encoded x264 at `crf`, with the source's colour matrix carried across the round trip explicitly (without that, BT.709 footage shifts every pixel, blended or not — measured, green 160 dropped to 142). Always check `alignment_preview` before committing to a long render; the dial-it-in pass is `scale_adjust`, `offset_x`, and `offset_y` on top of the auto result.

| Parameter | Default | Purpose |
|---|---|---|
| `base_video` | — | The full-body lipsync clip (the one with the small face). |
| `overlay_video` | — | The zoomed-in lipsync clip: same audio, same length, better face. |
| `align_mode` | `auto` | `auto` detects the head in both clips and matches them; `manual` uses the manual_* values. |
| `scale_adjust` | 1.0 | Multiplier on the auto-computed scale; raise slightly if the overlay face reads small. |
| `offset_x` / `offset_y` | 0 | Nudge the overlay in base-video pixels; positive is right/down. |
| `refine_alignment` | true | Phase-correlation correction after the detector estimate; auto-skipped when the renders are too dissimilar. |
| `coverage` | 1.0 | Fraction of the zoomed framing to use; lower tightens the blend around the face. |
| `edge_shape` | `ellipse` | Blend edge: `ellipse`, `rounded_rect`, or `rectangle`. |
| `feather_px` | 64 | Feather width in base-video pixels; generous is good, the two renders never match at the seam. |
| `color_match` | true | Per-frame LAB colour match of the overlay to the base beneath it. |
| `manual_scale` | 0.5 | `manual` mode: scale applied to the overlay. |
| `manual_center_x` / `manual_center_y` | 0 | `manual` mode: where the overlay's centre lands; 0 means the centre of the base frame. |
| `audio_source` | `base` | `base`, `overlay`, or `none`; both clips share the audio, so `base` is normally right. |
| `frame_tolerance` | 2 | Allowed frame-count difference; within it the longer tail passes through unblended, beyond it the run fails. |
| `crf` | 16 | x264 quality for the output. |
| `output_directory` | `""` | Optional folder for a file copy. |

Outputs: `composited_video`, `alignment_preview` (a mid-clip still with the detected head and blend boundary drawn — check it before a long render), and `placement`, JSON recording the computed scale, position, blend size, and detection details so a good alignment can be reused.

## 4. Extract Image Matte

Extract Image Matte pulls a soft-alpha matte and an RGBA cutout from a still image using rembg with BiRefNet-class models — local inference, no API, no secrets. It was built for matting the grey-backdrop AI masters, where hair edges and fuzzy knits need graded alpha rather than a hard silhouette; the outputs go straight to Resolve/Nuke or into the Composite nodes, and the matte's polarity (white = subject) matches both. This is the soft-edge complement to SAM-style segmentation: SAM gives hard per-object masks good for isolation and garbage mattes, this gives extraction-grade alpha. The first use of each model downloads its weights once, and model sessions are cached for the life of the engine process, so the first run per model is the slow one. If rembg is missing from the environment the node fails with instructions to refresh libraries rather than a bare ImportError.

Model choice is the main decision: `birefnet-portrait` (the default) has the softest hair edges for people, `birefnet-general` suits non-person subjects, and `isnet-general-use` / `u2net` are lighter and faster with coarser edges. `alpha_matting` adds an extra edge-refinement pass with fixed thresholds; the BiRefNet models rarely need it — try it if fine hair comes back hard. The node measures matte coverage and warns when the result is nearly empty (under 2% — the model may not have found a subject) or nearly full-frame (over 98% — the model may have kept the background), the usual signs of a wrong model choice. Directory copies are named after the source when it has a usable name (`<name>_cutout.png` / `<name>_matte.png`), so a folder of batch results stays traceable to its inputs.

| Parameter | Default | Purpose |
|---|---|---|
| `image` | — | The image to matte; a subject on an even studio backdrop mattes best. |
| `model` | `birefnet-portrait` | Matting model; see the guidance above. First use downloads weights once. |
| `alpha_matting` | false | Extra alpha-matting refinement on the mask edge, for stubborn fine hair. |
| `output_directory` | `""` | Optional folder for copies of both outputs, named after the source image. |

Outputs: `cutout_image` (the subject on transparency, RGBA PNG) and `matte_image` (white = subject, black = background, grey = soft edge).

## 5. Refine Video Matte

Refine Video Matte turns a hard, jagged matte video into a soft, edge-accurate alpha by letting the RGB clip's real edges shape it. It was built as the companion to SAM3 segmentation: SAM3's masks are temporally rock solid but hard-binary and blocky, and this node keeps the stability while fixing the edges — hair, motion blur, fabric all come from the RGB, not the mask. It works on any white-on-black matte source (keyer output, Magic Mask exports, RMBG). The mechanism is a guided filter (He et al.), implemented with plain OpenCV box filters so its cost per pixel is constant regardless of radius: within a band around the mask boundary, the alpha is re-derived from the greyscale RGB frame's local structure, so where the image has a soft edge the alpha becomes soft, and where it has a crisp edge the alpha snaps to it.

The parameters map directly onto the edge. `radius_px` is the refinement band's half-width — the distance around the mask boundary within which the RGB is allowed to reshape the alpha; bigger gives a softer, wider transition, and 8–12 suits 1080–2160 px content. `edge_softness` is the guided filter's epsilon: smaller hugs image edges harder (crisper), larger smooths more, typical range 0.0001–0.01. `matte_shift_px` grows (positive) or chokes (negative) the hard matte with a morphological dilate/erode before refinement — choke a couple of pixels when the source matte overshoots into the background, so the filter re-finds the edge from inside the subject rather than smoothing the overshoot. `in_black` and `in_white` are output levels applied after filtering: alpha at or below `in_black` becomes 0, which cleans residual background haze, and alpha at or above `in_white` becomes 1, which solidifies the subject core; `in_white` must exceed `in_black` or the run fails. The matte and RGB must be frame-locked twins — the matte should be extracted from this exact clip. A frame-count mismatch produces a warning and refines only the overlapping frames; a matte at a different resolution is resized to the RGB's. The output is encoded near-lossless (x264, CRF 10) so it survives another round of compositing.

| Parameter | Default | Purpose |
|---|---|---|
| `matte_video` | — | The hard matte to refine (white = subject), e.g. SAM3 output. |
| `rgb_video` | — | The colour clip the matte belongs to; its edges guide the refinement. |
| `radius_px` | 8 | Refinement band half-width in pixels; bigger = softer, wider edge. 8–12 for 1080–2160 px. |
| `edge_softness` | 0.001 | Guided-filter epsilon; smaller is crisper, larger smoother. Typical 0.0001–0.01. |
| `matte_shift_px` | 0 | Grow (+) or choke (−) the hard matte before refining. |
| `in_black` | 0.05 | Alpha at or below this becomes 0 — cleans background haze. |
| `in_white` | 0.95 | Alpha at or above this becomes 1 — solidifies the core. Must be greater than `in_black`. |
| `output_directory` | `""` | Optional folder for a file copy. |

Output: `refined_matte`, the soft-alpha matte video (white = subject) in a near-lossless encode, ready for Composite Over Background's `external` mode or a Resolve/Nuke comp.
