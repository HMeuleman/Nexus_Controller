"""Basic integration metadata checks."""

import ast
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
    assert manifest["version"] == "0.2.2"


def test_dutch_translation_has_all_config_and_options_fields():
    """The Dutch locale should cover every English setup and options field."""
    translations_path = (
        Path(__file__).parents[1] / "custom_components" / "nexus_controller" / "translations"
    )
    english = json.loads((translations_path / "en.json").read_text(encoding="utf-8"))
    dutch = json.loads((translations_path / "nl.json").read_text(encoding="utf-8"))

    assert (
        dutch["config"]["step"]["user"]["data"].keys()
        == english["config"]["step"]["user"]["data"].keys()
    )
    assert (
        dutch["options"]["step"]["init"]["data"].keys()
        == english["options"]["step"]["init"]["data"].keys()
    )
    assert dutch["entity"]["sensor"].keys() == english["entity"]["sensor"].keys()
    assert dutch["entity"]["sensor"]["battery_soc"]["name"].endswith("(SOC)")


def test_options_flow_does_not_override_read_only_config_entry_property():
    """OptionsFlow obtains config_entry from Home Assistant after initialization."""
    flow_path = (
        Path(__file__).parents[1] / "custom_components" / "nexus_controller" / "config_flow.py"
    )
    tree = ast.parse(flow_path.read_text(encoding="utf-8"))
    options_flow = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NexusOptionsFlow"
    )

    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"
        for node in options_flow.body
    )


def test_translations_include_all_phase_inputs_and_safety_outputs():
    """Every English and Dutch translation includes the new phase controls."""
    translations_path = (
        Path(__file__).parents[1] / "custom_components" / "nexus_controller" / "translations"
    )
    required_keys = {
        "grid_power_l1_entity",
        "grid_power_l2_entity",
        "grid_power_l3_entity",
        "charge_enable_entity",
        "charge_power_entity",
        "discharge_enable_entity",
        "discharge_power_entity",
        "fuse_limit_amps",
        "phase_headroom_watts",
    }
    for locale in ("en", "nl"):
        translation = json.loads((translations_path / f"{locale}.json").read_text(encoding="utf-8"))
        assert required_keys <= translation["config"]["step"]["user"]["data"].keys()
        assert "max_charge_power" in translation["entity"]["sensor"]
        assert "max_discharge_power" in translation["entity"]["sensor"]
