# Installing the HyperReal Nodes library

Step-by-step setup for teammates. When you're done, Griptape Nodes Desktop will have a **HyperReal Nodes** library with all our custom nodes (HeyGen, Topaz, WaveSpeed, Face Prep, Composite, Shot Settings, Upload to Spaces).

Nothing in these steps requires editing a file path in any config file — the only path involved is where *you* choose to clone the repo, and you pick it once in a file dialog.

**Requirements:** Griptape Nodes Desktop with engine version **0.86.0 or newer**, `git` installed, and access to the `production-hr/griptape-nodes-library-hyperreal` GitHub repo (you should already have this).

---

## 1. Clone the repo

Open a terminal and clone the repo anywhere you like — your usual projects folder is fine. It does **not** need to live inside your Griptape workspace directory.

```bash
git clone https://github.com/production-hr/griptape-nodes-library-hyperreal.git
```

Use your own GitHub credentials (the repo is shared with you). Remember where you put it — you'll browse to it in step 2.

> Don't clone onto a mapped or SUBST'd drive alias. Griptape resolves paths to their real location, and aliased drives break its path checks. Use the real drive path.

## 2. Register the library in Griptape

1. Open Griptape Nodes Desktop.
2. Go to **Settings → Libraries → + Add Library**.
3. Browse to your clone and select this file:

   ```
   <your clone>/hyperreal/griptape_nodes_library.json
   ```

   (Note: the manifest is inside the `hyperreal` subfolder, not at the repo root.)

Griptape reads the manifest and installs the library's Python dependencies into its own environment automatically — you don't need to pip-install anything yourself.

## 3. Add the API secrets

The API-backed nodes need keys. In Griptape, go to **Settings → API Keys & Secrets** and add the ones you'll use:

| Secret | Used by | Where it comes from |
|---|---|---|
| `HEYGEN_API_KEY` | HeyGen Avatar Video, HeyGen Video Translate | HeyGen dashboard → Settings → API |
| `TOPAZ_API_KEY` | Topaz Video Upscale, Topaz Image Upscale | Topaz Labs API dashboard |
| `WAVESPEED_API_KEY` | WaveSpeed Image Edit, InfiniteTalk, InfiniteTalk V2V | WaveSpeed dashboard |
| `DO_SPACES_KEY` / `DO_SPACES_SECRET` | Upload to Spaces | DigitalOcean → API → Spaces Keys |
| `DO_SPACES_REGION` | Upload to Spaces | e.g. `atl1` (region only) |

Get the shared key values from Tim through a secure channel (password manager or similar — not plain email/chat), or use your own accounts if you have them.

Notes:

- You only need the keys for the nodes you'll actually run. The local-processing nodes (**Face Prep, Composite, Overlay Zoomed Video, Zoom To Head, Shot Settings**) use no API and no secrets at all.
- For Spaces, `DO_SPACES_REGION` is enough — the endpoint is derived. If you set `DO_SPACES_ENDPOINT` instead, it must be the **region** endpoint (`https://atl1.digitaloceanspaces.com`), **never** the bucket URL the DO control panel shows.

## 4. Restart the engine (required)

Newly added secrets are only registered when the engine starts up — *Refresh Libraries is not enough for secrets*. Fully quit and relaunch Griptape Nodes Desktop (or restart the engine if you run it separately).

## 5. Verify

1. Open (or refresh) the node library panel.
2. You should see a **HyperReal Nodes** library with categories: Video → HeyGen / Topaz / WaveSpeed / Face Prep / Composite, Image → Topaz / Face Prep / WaveSpeed, Config → Shot, Storage → Spaces.
3. Drop a **HeyGen Avatar Video** node onto a flow. If it appears without errors, the install is good.
4. (Optional full check) Run a small graph: Load Image + audio → HeyGen Avatar Video. If it renders a lipsync clip, your `HEYGEN_API_KEY` is working.

First run of any Face Prep / Composite node downloads the bundled ffmpeg binaries once — a short one-time delay is normal.

---

## Staying up to date

When new nodes or fixes land:

```bash
git pull
```

then in Griptape use **Refresh Libraries** (or restart). Rule of thumb:

- **Node code changed** → Refresh Libraries is enough.
- **A new secret was added** to the library → add the key, then **restart the engine**.

## Troubleshooting

- **Library doesn't appear after adding** — confirm you selected `hyperreal/griptape_nodes_library.json` (inside the `hyperreal` subfolder), not a file at the repo root.
- **Node fails with an auth/key error** — the matching secret is missing or was added without an engine restart (step 4).
- **Upload to Spaces "works" but URLs look wrong** (bucket name doubled, or files land in a stray folder) — your `DO_SPACES_ENDPOINT` includes the bucket name. Use the plain region endpoint or just set `DO_SPACES_REGION`.
- **Engine version errors** — the library requires engine 0.86.0+. Update Griptape Nodes Desktop.
- **Workflows shared between machines** — our workflows use `{project_dir}` macros for portability, so they don't carry anyone's absolute paths. If a workflow references files, keep them inside the project directory.

For the full node-by-node reference (parameters, gotchas, verified behaviors), see the repo [README](../README.md).
