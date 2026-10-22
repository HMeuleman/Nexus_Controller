"""Basic integration metadata checks."""

import json
from pathlib import Path


def test_manifest_has_expected_domain_and_config_flow():
    """The custom component manifest should be installable by Home Assistant."""
    manifest_path = (
        Path(__file__).parents[1] / "custom_components" / "nexus_controller" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["domain"] == "nexus_controller"
    assert manifest["config_flow"] is True
