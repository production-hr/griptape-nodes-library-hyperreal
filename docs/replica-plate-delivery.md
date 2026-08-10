# Replica Plate Delivery — Green-Screen Training Footage

A short spec for producing and handing off the avatar **training footage** (the
"plate") that our conversational AI avatar is built from. Share this with anyone
generating or processing the source video.

> Revised 2026-08-08 after solving a keying failure on the Ozzy plate. The
> normative change is in **The target green** and **Where the green goes** — both
> sections are load-bearing, not stylistic. The previous recommendation (roto the
> subject out of a noisy generated background) is kept at the end as a fallback,
> with a note on why it is no longer the first resort.

## Purpose

We produce training footage for a conversational AI avatar (a Tavus CVI "replica"
of Ozzy). The performance is rendered against a **solid green background** so
that, at runtime, our web app can **chroma-key the green away** and composite the
avatar over anything — a solid color, a background image, or a black field for the
Proto Hologram display.

## Why a green plate

The avatar has to appear over **different** backgrounds depending on where it's
shown. Baking a removable green behind the subject lets us pull the avatar free
and place it anywhere, **live in the browser** (a WebGL chroma-key shader),
instead of locking it to one fixed background.

Tavus will also accept a plate with a background already baked in. We do not use
that, because it would mean retraining a replica every time the background needs
to change.

## The target green

**Use pure `RGB(0, 255, 0)`. Exactly that, on every replica render.**

Not "a saturated green" — that specific value. The runtime keyer measures
**green dominance** = `green − max(red, blue)` and applies a **brightness gate**,
and pure green maximises both at once:

| | Earlier Ozzy plate | Pure green |
|---|---|---|
| RGB | (0.7, 152.2, 106.1) | (0, 255, 0) |
| Green dominance (`G − max(R,B)`) | **46** | **255** |
| Brightness (V) | 152 | 255 |

That is a **5.5× wider margin** for the keyer to work in, and it is the difference
between an unusable key and a clean one.

Two things follow from it:

- **Generator flicker stops mattering.** An AI video generator reconstructs every
  frame, so a flat background comes back with frame-to-frame variation. That
  variation does not shrink when you widen the margin — it just stops crossing the
  threshold. At a dominance of 46 the wobble was a large fraction of the margin; at
  255 it is a small one.
- **Shadows and reflections survive the brightness gate.** At V=152 the background
  sat close to the luminance of a darkened shadow, so the gate could not reliably
  separate "background" from "shadow that should stay". At V=255 the background is
  at the ceiling and every shadow is unambiguously below it.

Watch out for **blue**, specifically. The keyer takes `max(red, blue)`, so a
cyan-leaning green eats the margin from the side you are not looking at. The
earlier plate had blue at 106 against green at 152 — nearly two-thirds — and that
alone cost most of the headroom.

Pure green is also the worst case for **edge spill** on anti-aliased detail. Check
fine silhouette elements (on the Ozzy plate, the bat wings) against a light
background. If spill shows, `CompositeOverBackground` has a `despill` parameter
that works independently of the matte source.

## Where the green goes

**Insert the green at the white-background stage, upstream of any AI generation.
Never try to produce or repair it downstream of a generator.**

The working process:

1. Start from the avatar still on its **original white background**.
2. Replace white with `RGB(0, 255, 0)` — a Resolve **Replace Color** does it
   cleanly, because white is uniform and the operation is well defined.
3. Feed that still to the lipsync generation (HeyGen Avatar IV).
4. Deliver the result.

Step 2 is where the contact shadows and floor reflections are preserved, and it
happens for free: Replace Color maps the flat white to flat green and leaves the
darker reflection as a *darker green*. That is exactly the form the runtime
brightness gate needs — a shadow that reads as a darker version of the background.
You get it by construction rather than trying to recover it later.

**What does not work:** picking a mid-brightness green, or trying to clean up a
generated background afterwards. Both were tried on the Ozzy plate. The generator
turns a flat mid green into a noisy, slightly shifted one, and no amount of keyer
tuning recovers it — the margin simply is not there.

## What "good" looks like

- **Flat, even, consistent green** — uniform across the frame *and* stable
  frame-to-frame (no flicker, banding, or texture).
- **`RGB(0, 255, 0)`**, identical across every replica render, so they all key with
  identical settings.
- **Saturated green that does not appear in the subject** — skin, dark clothing,
  and the black throne are all safe from a green key, so it separates cleanly.
- **Minimal compression** on anything destined for keying — ideally **4:4:4 or
  4:2:2**, not **4:2:0**. Chroma subsampling and lossy codecs (8-bit h.264) dump
  blocky "mosquito" noise into flat regions and wreck key edges. Prefer ProRes /
  high bitrate; work in a 10-bit+ timeline. This matters less at a dominance of 255
  than it did at 46 — blurred edge pixels still resolve confidently — but it is
  still free quality if the pipeline allows it.
- **Contact shadows and floor reflections are *darkened*, not removed** — so the
  keyer's brightness gate preserves them. This is what grounds the avatar instead
  of leaving it floating.
- **Subject well-lit and clearly separated** from the green (no heavy green spill
  on the subject; even lighting on the background).

## Deliverable

A clean green-screen clip of the performance:
- High-quality / low-compression (4:4:4 or 4:2:2 preferred).
- Background at `RGB(0, 255, 0)`, the same on every replica render.
- Subject evenly lit, separated from the background.
- Shadows/reflections darkened (not clipped) if they should survive the key.
- Ready to train the replica and be keyed live.

Preview any candidate plate through the local `/keytest` tool before committing to
a full replica training run. See `docs/greenscreen-tuning.md`.

## Fallback: rescuing a plate that was not authored this way

If you are handed a generated clip whose background is already noisy — no white
original, no chance to re-render — the green cannot be repaired by tuning the
keyer. Rebuild it instead:

1. Isolate the subject **by identity**, not by colour (Resolve Magic Mask / AI
   roto tracks the person regardless of background noise).
2. Composite over a clean green fill, or better, over the **original plate's
   background** if one exists — it already has the right green *and* the authored
   shadows, and it is valid for every frame as long as the camera and set are
   static.
3. Deliver that.

This works, but it is a manual round trip and the subject matte cuts at the
silhouette, so floor contact has to come from the original plate or be re-authored.
Authoring the green upstream (above) avoids all of it. Treat this as recovery, not
process.

## Key/runtime details (for reference)

- The runtime keyer measures **green dominance** = `green − max(red, blue)` and a
  **brightness gate**; a pixel is removed only when it's both green-dominant and
  bright. Darkening shadows drops them below the brightness gate so they survive.
- The app composites the keyed avatar over an operator-selected background (solid
  color / image / black for the hologram).
- HeyGen's `resolution: "1080p"` means the **short edge** is 1080 — portrait
  returns 1080×1920, landscape 1920×1080 — whatever the input size.
- Set the generator's aspect ratio **explicitly**. HeyGen's `auto` is documented as
  following the input image but was measured returning 16:9 for a portrait input,
  which pillarboxes the subject and bakes black bars into the frame.
