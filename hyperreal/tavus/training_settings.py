from __future__ import annotations

import re
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.traits.options import Options

REPLICA_TYPES = ["AI", "Human"]
DEFAULT_MODEL = "phoenix-3"
DEFAULT_BUCKET = "griptape"
DEFAULT_PREFIX = "tavus-training-videos/"
DEFAULT_NOTIFY_TO = "tim.coleman@hyperreal.io"

# (name, type, default, tooltip, options)
SETTINGS: list[tuple[str, str, Any, str, list[str] | None]] = [
    ("replica_name", "str", "", "Name for the replica in Tavus (also names the uploaded files).", None),
    ("replica_type", "str", "AI", "AI = synthetic subject. Human = real person: a consent video is REQUIRED "
     "(load it in the Consent Video node).", REPLICA_TYPES),
    ("model_name", "str", DEFAULT_MODEL, "Tavus replica model.", None),
    ("gaze_correction", "bool", False, "Ask Tavus to correct gaze toward camera.", None),
    ("background_green_screen", "bool", False, "Training video was shot on green screen.", None),
    ("spaces_bucket", "str", DEFAULT_BUCKET, "DigitalOcean Spaces bucket the videos are uploaded to.", None),
    ("spaces_key_prefix", "str", DEFAULT_PREFIX, "Folder inside the bucket.", None),
    ("notify_to", "str", DEFAULT_NOTIFY_TO, "Notification recipient(s), comma-separated.", None),
    ("notify_from", "str", "", "Sender address. Empty = the SMTP_USER account.", None),
    ("email_note", "str", "", "Optional note included in the notification emails (project, requester, ...).", None),
]


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_")
    return slug or "replica"


class TavusTrainingSettings(DataNode):
    """One place to set everything a Tavus training run needs.

    Replaces the Google Sheet row: name, type, model, flags, storage target, and
    notification addresses live here and are wired to the upload, train, status and
    email nodes. Also derives tidy object names for the uploaded training/consent
    videos so the bucket stays organized. A DataNode: resolves as a dependency, no
    control wiring.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "video/tavus",
            "description": "One-stop settings for a Tavus replica training run, wired to every node that needs them.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        for pname, ptype, default, tip, options in SETTINGS:
            param = Parameter(
                name=pname,
                type=ptype,
                default_value=default,
                tooltip=tip,
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
            if options:
                param.add_trait(Options(choices=options))
            self.add_parameter(param)

        for out_name, tip in (
            ("training_filename", "Derived object name for the training video: <replica_name>_training.mp4"),
            ("consent_filename", "Derived object name for the consent video: <replica_name>_consent.mp4"),
        ):
            self.add_parameter(
                Parameter(name=out_name, output_type="str", tooltip=tip, allowed_modes={ParameterMode.OUTPUT})
            )

    def process(self) -> None:
        """Publish the properties as outputs and derive the file names. No side effects."""
        for pname, _ptype, default, _tip, _opts in SETTINGS:
            self.parameter_output_values[pname] = self.parameter_values.get(pname, default)
        base = _slug(str(self.parameter_values.get("replica_name") or ""))
        self.parameter_output_values["training_filename"] = f"{base}_training.mp4"
        self.parameter_output_values["consent_filename"] = f"{base}_consent.mp4"
