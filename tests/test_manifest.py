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


def test_dutch_translation_has_all_config_and_options_fields():
    """The Dutch locale should cover every English setup and options field."""
    translations_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "nexus_controller"
        / "translations"
    )
    english = json.loads((translations_path / "en.json").read_text(encoding="utf-8"))
    dutch = json.loads((translations_path / "nl.json").read_text(encoding="utf-8"))

    assert dutch["config"]["step"]["user"]["data"].keys() == english["config"]["step"][
        "user"
    ]["data"].keys()
    assert dutch["options"]["step"]["init"]["data"].keys() == english["options"]["step"][
        "init"
    ]["data"].keys()
    assert dutch["entity"]["sensor"].keys() == english["entity"]["sensor"].keys()
    assert dutch["entity"]["sensor"]["battery_soc"]["name"].endswith("(SOC)")
