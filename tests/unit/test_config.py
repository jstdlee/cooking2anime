from pathlib import Path

import pytest
import yaml

from c2a.config import Config, load_config, parse_set
from c2a.core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def test_default_yaml_equals_builtin_defaults() -> None:
    # configs/default.yaml is documentation; it must never drift from the code.
    assert load_config(ROOT / "configs" / "default.yaml") == Config()


def test_unknown_key_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"output": {"resolutoin": "1080p"}}))
    with pytest.raises(ConfigError, match="resolutoin"):
        load_config(p)


def test_invalid_value_is_an_error() -> None:
    with pytest.raises(ConfigError):
        load_config(None, {"output": {"resolution": "4k"}})


def test_parse_set_nested_and_typed() -> None:
    assert parse_set(["output.resolution=1080p", "audio.voice.enabled=false",
                      "output.crf=20"]) == {
        "output": {"resolution": "1080p", "crf": 20},
        "audio": {"voice": {"enabled": False}},
    }
    with pytest.raises(ConfigError):
        parse_set(["novalue"])


def test_overrides_merge_with_file(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"output": {"crf": 20}}))
    cfg = load_config(p, {"output": {"resolution": "1080p"}})
    assert cfg.output.crf == 20 and cfg.output.resolution == "1080p"
