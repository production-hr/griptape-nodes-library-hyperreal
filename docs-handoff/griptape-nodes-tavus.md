---
title: Tavus Replica Training
section: Pipeline
subsection: Griptape Nodes
category: Tavus Replica Training
excerpt: Three Tavus nodes and two workflows that train and monitor conversational replicas.
tags: [ai, video, workflow, automation, training]
updated: 2026-09-18
---
> lede: How to launch a Tavus conversational-replica training run from Griptape Nodes and collect the result hours later, without babysitting either end.

Training a Tavus replica takes two to three hours on Tavus's servers, which is far too long to hold a single workflow open. The pipeline therefore splits the job into two separate runs: `tavus_launch` uploads the video(s), submits the training request, and emails you the replica ID; `tavus_check_training` takes that ID whenever you come back and emails you the outcome — including Tavus's own error text if the training failed. Three HyperReal nodes carry the work: **Tavus Training Settings** (one config node wired to everything), **Tavus Train Replica** (the submission), and **Tavus Replica Status** (the check). This setup replaced the old n8n "Replica Training Manager" and its Google Sheet of settings rows.

## 1. The Shape of the Pipeline

Tavus does not accept file uploads. Its API takes a **public http(s) URL** and downloads the video from its own servers, so a local path or a `localhost` URL can never work — Train Replica refuses both up front with an error that says so, before any API call is made. That is why the launch workflow routes every video through **Upload to Spaces** (DigitalOcean Spaces, `public=true`) first: the upload's `url` output is exactly what Tavus needs.

Both Tavus nodes authenticate with the secret **`TAVUS_API_KEY`** (Settings → API Keys & Secrets, then restart the engine, as with any new secret) and talk to `https://tavusapi.com` — Train Replica does `POST /v2/replicas`, Replica Status does `GET /v2/replicas/{id}?verbose=true`. Both retry automatically on rate limiting (HTTP 429), honoring the `Retry-After` header. Both are standard success/failure nodes with the usual execution and status outputs, which this page does not repeat per node.

> **Note:** The email legs use the Send Email node. Its SMTP secrets (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`) are documented on the Utilities page and are not repeated here.

## 2. Tavus Training Settings

A data node — no control wiring — that is the one place to type everything a training run needs: name, type, model, flags, storage target, and notification addresses. Every other node in the launch graph reads from it, so a new run means editing one node, not five. It also derives tidy object names for the uploaded files (`<replica_name>_training.mp4` and `<replica_name>_consent.mp4`, with the name slugged to safe characters) so the Spaces bucket stays organized. Without this node, the same strings would be typed into the upload, train, and email nodes separately and would inevitably drift.

| Parameter | Default | Purpose |
|---|---|---|
| `replica_name` | (empty) | Name for the replica in Tavus; also names the uploaded files. |
| `replica_type` | `AI` | `AI` = synthetic subject. `Human` = real person; a consent video is required. |
| `model_name` | `phoenix-3` | Tavus replica model. |
| `gaze_correction` | `false` | Ask Tavus to correct the subject's gaze toward camera. |
| `background_green_screen` | `false` | Tell Tavus the training video was shot on green screen. |
| `spaces_bucket` | `griptape` | DigitalOcean Spaces bucket the videos are uploaded to. |
| `spaces_key_prefix` | `tavus-training-videos/` | Folder inside the bucket. |
| `notify_to` | `tim.coleman@hyperreal.io` | Notification recipient(s), comma-separated. |
| `notify_from` | (empty) | Sender address; empty means the `SMTP_USER` account. |
| `email_note` | (empty) | Optional free-text note (project, requester, …). |

It also publishes two derived outputs, `training_filename` and `consent_filename`, which the launch workflow wires into the two upload nodes.

## 3. Tavus Train Replica

Submits the training request and hands back the **`replica_id`**. Keep that ID — it *is* the replica; the check workflow, and any later use of the replica, needs it. The node validates the video URLs before calling Tavus: an empty URL, a non-http(s) value, or anything pointing at `localhost`/`127.0.0.1` fails immediately with a message telling you to upload to Spaces first.

The **AI vs Human rule** is enforced here. If `replica_type` is `Human`, a consent video URL must be present — Tavus requires a consent statement for real people — and the run fails with a readable error if it is missing. If `replica_type` is `AI`, any loaded consent video is simply not sent, and the result details note that it was ignored. A separate consent clip is only needed when the consent statement is not spoken inside the training video itself.

| Parameter | Default | Purpose |
|---|---|---|
| `train_video_url` | (empty) | Public URL of the training video (wire Upload to Spaces `url`). |
| `replica_name` | (empty) | Name shown in the Tavus dashboard (sent only if non-empty). |
| `consent_video_url` | (empty) | Public URL of a separate consent-statement video; required for Human, ignored for AI. |
| `model_name` | `phoenix-3` | Tavus replica model (sent only if non-empty). |
| `replica_type` | `AI` | `AI` or `Human` — see the rule above. |
| `gaze_correction` | `false` | Sent as `properties.gaze_correction`. |
| `background_green_screen` | `false` | Sent as `properties.background_green_screen` so Tavus can key the footage. |
| `callback_url` | (empty) | Optional webhook Tavus calls on completion; usually left empty in favor of Replica Status. |
| `replica_id` | — (output) | The new replica's ID — keep it. |
| `status` | — (output) | Initial status from Tavus (normally `started`). |
| `response` | — (output) | Raw Tavus JSON response. |
| `launch_summary` | — (output) | Ready-to-send plain-text summary — wire into Send Email `body`. |

The `launch_summary` output is a complete email body: name, type, model, replica ID, flags, both video URLs, submission time, and a reminder that training takes 2–3 hours and which workflow to run next.

## 4. Tavus Replica Status

Fetches a replica's training record by ID: status, progress (e.g. `22/100`), registered name, preview thumbnail URL once one exists, error message, and the full record. By default it checks once and returns. With `wait_until_complete` on, it polls at `poll_interval_seconds` until the status is terminal (`completed`/`ready`, or `error`/`failed`) or `max_wait_minutes` elapses; hitting the deadline is not a failure — the replica keeps training on Tavus's side, and the node says to run it again later.

The important behavior for the check workflow: the node publishes **all of its outputs before deciding success or failure**. When Tavus reports `error`/`failed`, the node itself fails — with Tavus's `error_message` in the details, so the reason is visible on the canvas — but `status_summary` and the other outputs are already populated. That is what lets a downstream email still carry the full failure report.

| Parameter | Default | Purpose |
|---|---|---|
| `replica_id` | (empty) | The ID from Tavus Train Replica or the Tavus dashboard. |
| `wait_until_complete` | `false` | Keep polling until training completes or errors; off = one check. |
| `poll_interval_seconds` | `60` | Seconds between polls (values below 5 are clamped to 5). |
| `max_wait_minutes` | `360` | Give up waiting after this long; check again later. |
| `status` | — (output) | Tavus training status, e.g. `started` / `training` / `completed` / `error`. |
| `training_progress` | — (output) | Progress string as Tavus reports it, e.g. `40/100`. |
| `replica_name` | — (output) | Name as registered with Tavus. |
| `thumbnail_video_url` | — (output) | Preview clip URL once Tavus provides one. |
| `error_message` | — (output) | Tavus error text when training failed; empty otherwise. |
| `replica` | — (output) | Full replica record (JSON). |
| `status_summary` | — (output) | Ready-to-send plain-text summary — wire into Send Email `body`. |

An unknown ID gets a specific error ("Tavus has no replica with that id") rather than a raw 404.

## 5. Workflow: tavus_launch

**Purpose:** upload the training (and, for Human runs, consent) video to Spaces, submit the training job to Tavus, and email the launch summary. **Output:** the replica ID, on the canvas and in the "Tavus replica training LAUNCHED" email.

```
Training Settings ──(bucket, prefix, filenames, name, type,
     │              model, flags, notify to/from)──► everything below
     │
Load Video (training) ──► Upload Training Video ─┐  exec chain:
Load Video (consent,     Upload Consent Video ───┤  Upload Training
  Human only)              (allow_empty = true)  │  → Upload Consent
                                                 ▼  → Train Replica
                              url ──► Tavus Train Replica ──► Launched
                              url ──► (consent_video_url)     Email
                                      launch_summary ──► body
```

To run: set the fields on **Training Settings**, load the training video in the **Training Video** Load Video node, load a consent clip in **Consent Video (Human only)** if and only if the type is `Human`, and run the flow. Both uploads are `public=true`; the consent uploader has `allow_empty` on, so an AI-type run with an empty consent node passes through cleanly instead of failing the upload — that is the whole reason the consent branch can stay on the canvas permanently. Train Replica then ignores the empty consent URL for AI, or requires it for Human.

Nodes used: Tavus Training Settings, two Load Video (Griptape core), two Upload to Spaces, Tavus Train Replica, Send Email. The settings node feeds the uploads (bucket, prefix, derived filenames), the train node (name, type, model, both flags), and the email (`notify_to`, `notify_from`); `launch_summary` becomes the email body and the subject is fixed at "Tavus replica training LAUNCHED".

> **Note:** In the current graph the settings node's `email_note` is set but not wired into the email — treat it as a scratch field until that connection is added.

## 6. Workflow: tavus_check_training

**Purpose:** fetch the training result for a replica and email it. **Output:** a "Tavus replica training result" email containing the status summary — status, progress, preview URL, and any error.

```
Tavus Replica Status ── status_summary ──► body ── Result Email
        │ exec_out ───────────────────────► exec_in ──┘
        │ failure  ───────────────────────► exec_in ──┘
```

To run: paste the replica ID (from the launch email or the Tavus dashboard) into **Replica Status** and run. As saved, `wait_until_complete` is off, so it is a single check you can rerun at will; turn it on (default 60 s interval, 360 min cap) to block until training finishes.

The graph's one structural trick is that **both** the success (`exec_out`) and failure (`failure`) control outputs of Replica Status are wired into the email's `exec_in`. A completed training mails the completion summary; a *failed* training — where the status node itself fails, carrying Tavus's `error_message` — still reaches the email, whose body is the already-published `status_summary` with the error text in it. Without the failure connection, a failed training would end the flow silently and nobody would be told.

Nodes used: Tavus Replica Status and Send Email (recipient typed directly on the email node; subject fixed at "Tavus replica training result").
