from c2a.stages.audio import VOICE_DIRECTIONS


def test_voice_directions_are_sound_only() -> None:
    # Drama 3 must not speak words: directions are a single bracketed cue, nothing else
    for d in VOICE_DIRECTIONS.values():
        assert d.startswith("[") and d.endswith("]") and d.count("[") == 1
