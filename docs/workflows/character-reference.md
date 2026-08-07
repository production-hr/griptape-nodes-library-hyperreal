# Workflow: Character Reference Generation

**Location:** `docs/workflows/character-reference.md`
**Status:** Design complete, not yet built in the engine.
**Custom nodes required:** none. One small amendment to an existing node is recommended (§9.1).

Two related deliverables, one graph:

1. **Character model sheet** — head views + body views (+ optional accessory panel) assembled into a single reference sheet.
2. **Character head reference set** — one image per camera angle, plus a composited contact sheet for review.

They are the same pipeline with different prompt lists. Build the head-reference version first; the model sheet is that graph plus a second loop and a vertical merge.

---

## 1. Verification status

Everything in §2 was read from `docs.griptapenodes.com/en/stable/` on 2026-08-07. Everything in §4–§6 is a design proposal to be tuned against real output.

| Claim | Status |
|---|---|
| `ForEach Group` exists; exposes `current_item` / `index`; collects `new_item_to_add` into `results`; has Execution Mode (sequential / all-at-once) and Testing Mode | **Verified** — `guides/editor/node_groups/` |
| `Retry Group` exists; wires to Succeeded/Failed control inputs; `Max Iterations` default 3; `Raise on failure` toggle | **Verified** — same page |
| `Display Image Grid` **outputs an ImageUrlArtifact** (not preview-only); `columns` 1–10, `spacing`, `crop_to_fit`, `background_color`, `transparent_bg`, `border_radius`, preset sizes to 4K | **Verified** — `nodes/image/display_image_grid/` |
| Workflow variables support inline `{name}` substitution in text parameters, with format specs (`:slug`, `:snake`, etc.); resolved at execution time against variables merged with project macros | **Verified** — `guides/variables/` |
| Only `str` and `int` variables substitute inline | **Verified** — same page |
| `Merge Images`, `Create List`, `Create Text List`, `JSON Extract Value`, `Add Text to Existing Image`, `Save Image` exist in the standard library | **Verified** (presence only — parameter names not read) |
| `{character_name:slug}` resolves inside `WaveSpeedImageEdit.output_directory` | **Unverified** — see §9.2 |
| `Merge Images` parameter names and vertical-stack behaviour | **Unverified** — see §9.3 |
| Prompt wording, view lists, model settings | **Proposal** — expect iteration |

---

## 2. Design rationale

### 2.1 Generate views individually, never as one sheet

Do not prompt for a model sheet in a single generation. Generate each view as its own full-frame image and assemble afterward.

- **Resolution.** A 6-panel sheet at 4K yields ~1000px per head. The same head generated alone at 2K yields the full 2K. Downstream consumers (HeyGen Avatar IV source images, Face Prep §6 crops, face-swap references) sample from these — the difference is the entire point of the artifact.
- **Retry granularity.** A wrong ear in the profile view costs one re-run, not a whole sheet.
- **The individual files are a requirement anyway.** Both deliverables call for per-view images. One pipeline serves both.

### 2.2 Anchor-then-fan-out

Generate the front view first from the source photographs alone. Feed that generated image back in as an additional reference for every subsequent view.

This converts the task from "interpret these photos eight independent times" into "rotate a thing you already produced." It is the largest single quality lever in the design and costs one extra generation.

Because the anchor is produced *outside* the ForEach group, all views can still run concurrently.

### 2.3 Two prompt rules that matter more than they look

- **Never describe the face in words.** No "brown eyes, strong jaw." Descriptive identity language pushes the model from *preserving* toward *generating*, and identity drifts across views. References carry identity; prompts carry only camera and pose.
- **Phrase it so the camera moves, not the subject.** "The camera has moved 45 degrees to the subject's left" — not "the subject turns." Otherwise the model rotates the head and changes expression instead of orbiting.

---

## 3. Graph shape

```
Create Variable (character_name)  ──┐
Create Variable (style_block)     ──┤   referenced as {character_name}, {style_block}
                                    │   inside prompt fields and path fields
Load Image  x N  (face/hair refs) ──┤
Load Image  x N  (costume refs)   ──┤
                                    │
                    ┌───────────────┴──────────────┐
                    │  ANCHOR                      │
                    │  WaveSpeedImageEdit          │  front view, full resolution
                    │  google/nano-banana-pro/edit │
                    └───────────────┬──────────────┘
                                    │ anchor_image
Create List (view specs, JSON)  ────┤
                                    ▼
        ╔═══════════════════════════════════════════════╗
        ║  ForEach Group — items = view specs           ║
        ║                                               ║
        ║  current_item ─► JSON Extract "view"  ───┐    ║
        ║  current_item ─► JSON Extract "slug"  ─┐ │    ║
        ║                                       │ │    ║
        ║   ╔═══════════════════════════════╗   │ │    ║
        ║   ║ Retry Group (max 3)           ║   │ │    ║
        ║   ║   WaveSpeedImageEdit          ║◄──┴─┘    ║
        ║   ║   images = [refs..., anchor]  ║          ║
        ║   ╚═══════════════╤═══════════════╝          ║
        ║                   │                          ║
        ║          Save Image ({slug})                 ║
        ║                   └──► new_item_to_add       ║
        ╚═══════════════════════╤═══════════════════════╝
                                │ results (list of images)
                                ▼
                    Display Image Grid ──► Save Image (contact sheet)
```

**Model sheet variant:** duplicate the ForEach group for body views (different list, different aspect ratio), run a second `Display Image Grid`, then `Merge Images` vertically to stack head section over body section. Add a third loop and a third grid for accessories when the character has them.

### 3.1 Reference image ordering

`WaveSpeedImageEdit.images` is a ParameterList capped at 14 for the google models, 16 for gpt-image-2. Order matters — later references bias more strongly. Use:

1. Identity references (face, 2–3 images)
2. Hair / back-of-head reference
3. Costume references (2–3 images)
4. **Anchor image last**

The loop prompt (§5.2) explicitly says "the final reference image," which depends on this ordering.

### 3.2 Node settings

| Node | Setting | Value |
|---|---|---|
| WaveSpeedImageEdit (anchor + head views) | `model` | `google/nano-banana-pro/edit` |
| | `resolution` | `2k` (or `4k` if head crops feed Face Prep) |
| | `aspect_ratio` | `1:1` |
| | `output_format` | `png` |
| WaveSpeedImageEdit (body views) | `aspect_ratio` | `2:3` |
| ForEach Group | Execution Mode | **sequential** initially; all-at-once once prompts are stable |
| | Testing Mode | on, while iterating on a single view |
| Retry Group | Max Iterations | 3 |
| Display Image Grid | `crop_to_fit` | **false** (defaults true; will trim carefully framed heads) |
| | `columns` | 3 |
| | `background_color` | `#808080` (matches the plate background) |
| | `output_image_size` | preset, 4K |

Keep aspect ratio uniform *within* each grid — 1:1 for all head views, 2:3 for all body views — or the cells will not align.

---

## 4. Style block

One `Create Variable`, type `str`, name `style_block`. Referenced as `{style_block}` at the end of every prompt.

```
Neutral studio lighting: flat, even, frontal fill with no rim light and no
dramatic shadow. Plain seamless mid-grey background, no gradient, no vignette,
no floor line. Subject centred and fully in frame. Sharp focus throughout, no
depth-of-field blur. Photographic realism, no stylisation, no illustration,
no beautification. 85mm-equivalent lens, no wide-angle distortion. Preserve the
person's identity exactly: facial structure, skin tone, skin texture, blemishes,
hairline, and hair styling as in the reference images. Preserve garment colour,
pattern, cut, and construction exactly. No text, no watermarks, no props.
```

---

## 5. Prompt library — head views

### 5.1 Anchor prompt

Generated first, outside the loop, from source photographs only.

```
Using the reference photographs, produce a single front-facing head-and-shoulders
portrait of the same person. Camera dead-on at eye level, subject looking directly
into the lens. Head perfectly level: no tilt, no roll, no turn. Neutral relaxed
expression, mouth closed, eyes open, eyebrows at rest. Both ears visible.
Shoulders square to camera, cropped mid-chest.
{style_block}
```

### 5.2 Loop prompt template

Assembled inside the ForEach group. `{view}` comes from `JSON Extract Value` on `current_item`.

```
The final reference image shows the subject in a front view. Reproduce that exact
person, hair, and wardrobe from a different camera angle.

{view}

Framing must match the front view exactly: identical crop, identical head size in
frame, identical distance from camera, identical lighting, identical background.
Neutral relaxed expression, mouth closed. Nothing about the subject changes except
the angle from which they are seen.
{style_block}
```

### 5.3 Head view list

`Create List` of JSON objects. Numeric prefixes in the slug control both filename sort order and grid fill order.

```json
[
  {
    "slug": "head_00_front",
    "view": "The camera is directly in front of the subject at eye level. The subject looks into the lens."
  },
  {
    "slug": "head_01_3q_left",
    "view": "The camera has moved 45 degrees to the subject's left, remaining at eye level. The subject's head and gaze remain fixed forward relative to their shoulders; only the camera position has changed. The far eye and far cheekbone remain visible."
  },
  {
    "slug": "head_02_3q_right",
    "view": "The camera has moved 45 degrees to the subject's right, remaining at eye level. The subject's head and gaze remain fixed forward relative to their shoulders. The far eye and far cheekbone remain visible."
  },
  {
    "slug": "head_03_profile_left",
    "view": "The camera has moved 90 degrees to the subject's left, remaining at eye level: a full profile. The nose, lips, and chin read as a clean silhouette against the background. One ear fully visible."
  },
  {
    "slug": "head_04_profile_right",
    "view": "The camera has moved 90 degrees to the subject's right, remaining at eye level: a full profile. The nose, lips, and chin read as a clean silhouette against the background. One ear fully visible."
  },
  {
    "slug": "head_05_rear_3q",
    "view": "The camera has moved 135 degrees to the subject's left, remaining at eye level, looking at the back and side of the head. The hair's crown, part, and nape are clearly visible. A sliver of cheek and ear is visible at the frame's edge."
  },
  {
    "slug": "head_06_high_angle",
    "view": "The camera is directly in front of the subject but raised 30 degrees above eye level, angled down. The subject's head remains level and does not tilt up to follow the camera."
  },
  {
    "slug": "head_07_low_angle",
    "view": "The camera is directly in front of the subject but lowered 30 degrees below eye level, angled up. The subject's head remains level and does not tilt down to follow the camera."
  }
]
```

---

## 6. Prompt library — body views

Second ForEach group, `aspect_ratio: 2:3`.

### 6.1 Loop prompt template

```
Full-body view of the same person, standing straight, feet shoulder-width apart,
weight evenly distributed on both legs. Arms relaxed and held roughly 15 degrees
away from the torso, palms facing inward toward the thighs, fingers relaxed and
separated. Head level, neutral expression. Full figure in frame from crown to
below the feet, with even margin above and below.

{view}

{style_block}
```

**On the A-pose:** arms flat against the body occlude side seams, pocket details, and the hands themselves — exactly the parts of a costume a reference sheet exists to document. The 15-degree offset is standard turnaround practice and costs nothing.

### 6.2 Body view list

```json
[
  {
    "slug": "body_00_front",
    "view": "The camera is directly in front of the subject at chest height. The subject faces the camera squarely."
  },
  {
    "slug": "body_01_3q_left",
    "view": "The camera has moved 45 degrees to the subject's left, remaining at chest height. The subject's stance and posture are unchanged; only the camera position has changed."
  },
  {
    "slug": "body_02_3q_right",
    "view": "The camera has moved 45 degrees to the subject's right, remaining at chest height. The subject's stance and posture are unchanged."
  },
  {
    "slug": "body_03_profile_left",
    "view": "The camera has moved 90 degrees to the subject's left, remaining at chest height: a full side view. The garment's side seam and silhouette are clearly visible."
  },
  {
    "slug": "body_04_profile_right",
    "view": "The camera has moved 90 degrees to the subject's right, remaining at chest height: a full side view."
  },
  {
    "slug": "body_05_back",
    "view": "The camera has moved 180 degrees behind the subject, remaining at chest height. The full back of the garment and the back of the head are visible."
  }
]
```

---

## 7. Prompt library — accessory panel (optional)

Third loop, no person in frame. Items list is per-character, e.g. `["wide-brimmed straw hat", "leather satchel", "beaded necklace"]`.

```
Isolated product photograph of the {item} worn by the subject in the reference
images, removed from the body and photographed alone. Three-quarter view on a
plain seamless mid-grey background, even studio lighting, sharp focus, full item
in frame. Reproduce the item's colour, material, hardware, and construction
exactly as seen in the references.
```

Aspect ratio `1:1` to match the head grid's cell shape.

---

## 8. Output organization

One variable drives everything. `Create Variable` → `character_name` = e.g. `"Ana Reyes"`.

Path fields then read:

```
{project_dir}/characters/{character_name:slug}/02_head_views
```

### Directory layout

```
characters/ana-reyes/
  00_refs/          source photographs (Load Image nodes point here)
  01_anchor/        anchor_front.png
  02_head_views/    head_00_front.png … head_07_low_angle.png
  03_body_views/    body_00_front.png … body_05_back.png
  04_accessories/
  05_sheets/        head_contact_sheet.png, model_sheet.png
```

### Sheet assembly

- `Display Image Grid` #1 — head views, 3 columns, `crop_to_fit: false`
- `Display Image Grid` #2 — body views, 3 columns, same settings
- `Display Image Grid` #3 — accessories, when present
- `Merge Images` (vertical) — stack the grids into the final model sheet
- `Add Text to Existing Image` — optional FRONT / 3⁄4 / PROFILE labels
- `Save Image` → `05_sheets/`

---

## 9. Open items and known risks

### 9.1 Add `seed` to `WaveSpeedImageEdit` (recommended before production use)

SPEC §5 gives `seed` to the InfiniteTalk nodes but not the image-edit node. Reference-sheet work depends on reproducibility: you will want to re-run view 3 with a tweaked prompt while holding everything else constant. Confirm whether the nano-banana and gpt-image edit endpoints accept a seed parameter; if they do, add it to the node before relying on these workflows.

### 9.2 Variable substitution in `output_directory` — unverified

The Variables docs state that `{name}` tokens in *any* parameter value are resolved at execution time against workflow variables merged with project macros. That should make `{character_name:slug}` work in `output_directory` for free, since the node is already macro-aware. **Test this on one run before adopting the pattern.** If it does not resolve there, fall back to a `Merge Texts` node building the path string and wiring it into `output_directory`.

### 9.3 `Merge Images` parameters — unverified

Presence in the standard library is confirmed; the parameter names and exact vertical-stack behaviour were not read. Verify when wiring. If it turns out not to support vertical stacking of differently sized inputs, the fallback is a single `Display Image Grid` over all views with a uniform aspect ratio, losing the section separation.

### 9.4 Execution mode and spend

All-at-once fires 8 concurrent WaveSpeed calls per loop. Architecturally fine, but check the rate limit and get a feel for per-image cost before flipping it. Start sequential.

### 9.5 Identity drift on rear and profile views

If the rear view invents a hairstyle, the fix is a reference photograph of the actual back of the head — not more prompt text. Budget for this in the intake checklist for each character.

### 9.6 Deferred

- **Automatic view QA.** A `Describe Image` pass checking that each output matches its requested angle. Plausible, unproven, and probably slower than eyeballing the contact sheet at this batch size.
- **Expression variants.** Neutral only for now. A second axis (neutral / smiling / speaking) multiplies the grid and is out of scope until there is a real need.
- **Batch across multiple characters.** Out of scope per the standing 1–5 ad-hoc jobs rule.

---

## 10. Build order

1. Wire the anchor generation alone. Confirm identity preservation against the source photos before anything else.
2. Add the ForEach group with **Testing Mode on**, running index 1 (`head_01_3q_left`) only. Tune the loop prompt template here — this is where most of the iteration happens.
3. Turn Testing Mode off, run the full head list sequentially.
4. Add `Display Image Grid` and confirm the contact sheet.
5. Add the Retry Group wrapper.
6. Switch Execution Mode to all-at-once and confirm nothing rate-limits.
7. Duplicate for body views; add `Merge Images` for the stacked model sheet.

Step 2 is the whole job. Steps 3–7 are mechanical.

---

## 11. References

- Node Groups (ForEach, Retry, Subflow): `docs.griptapenodes.com/en/stable/guides/editor/node_groups/`
- Display Image Grid: `docs.griptapenodes.com/en/stable/nodes/image/display_image_grid/`
- Workflow Variables and inline substitution: `docs.griptapenodes.com/en/stable/guides/variables/`
- Macros (path templates, format specs, sequence slots): `docs.griptapenodes.com/en/stable/guides/projects/macros/`
- `WaveSpeedImageEdit` node contract: `SPEC.md` §5
