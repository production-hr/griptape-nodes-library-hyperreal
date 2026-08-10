from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.traits.options import Options

# "auto" is deliberately absent. HeyGen's own docs describe it as following the
# input image; in practice it returned 16:9 for a portrait input, which
# pillarboxes the subject and silently poisons a downstream overlay. A shot's
# aspect is a decision, so make it one.
ASPECT_RATIOS = ["9:16", "16:9", "4:5", "5:4", "1:1"]
RESOLUTIONS = ["1080p", "720p"]
EXPRESSIVENESS_LEVELS = ["low", "medium", "high"]


class ShotSettings(DataNode):
    """One place to set the values that several nodes in a shot must agree on.

    A two-pass lipsync is only correct if both passes share an aspect ratio, a
    resolution and an expressiveness level. Set on each node individually, those
    are three chances to diverge, and a divergence does not announce itself — it
    shows up as a broken composite after both generations have been paid for.
    Wiring them from one node makes agreement structural instead of remembered.

    A DataNode, so it resolves as a dependency of whatever reads it; there is no
    control wiring and nothing to run in order.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "config/shot",
            "description": "Shared shot settings (aspect, resolution, expressiveness) wired to every node "
            "that must agree on them.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="aspect_ratio",
                output_type="str",
                type="str",
                default_value="9:16",
                tooltip="Aspect ratio for every generated clip in this shot. Wire to the lipsync nodes. "
                "Set it explicitly — leaving a generator on 'auto' is what pillarboxes a portrait subject.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                traits={Options(choices=ASPECT_RATIOS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="resolution",
                output_type="str",
                type="str",
                default_value="1080p",
                tooltip="Render resolution for every generated clip in this shot.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                traits={Options(choices=RESOLUTIONS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="expressiveness",
                output_type="str",
                type="str",
                default_value="low",
                tooltip="Motion level for every lipsync pass. Keep it low for a two-pass overlay: camera "
                "drift differs between generations and makes the zoomed face slide against the base.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
                traits={Options(choices=EXPRESSIVENESS_LEVELS)},
            )
        )
        self.add_parameter(
            Parameter(
                name="upscale_long_edge",
                output_type="int",
                type="int",
                default_value=1920,
                tooltip="Target long edge for the still upscale before the zoomed lipsync pass. Wire to "
                "Topaz Image Upscale.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="output_directory",
                output_type="str",
                type="str",
                default_value="",
                tooltip="Folder every node in the shot saves a copy into, e.g. {project_dir}/outputs. "
                "Blank means each node keeps its own setting.",
                allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        """Publish the properties as outputs. No work, no side effects — just a shared source."""
        for name in ("aspect_ratio", "resolution", "expressiveness", "upscale_long_edge", "output_directory"):
            self.parameter_output_values[name] = self.parameter_values.get(
                name, self.get_parameter_by_name(name).default_value
            )
