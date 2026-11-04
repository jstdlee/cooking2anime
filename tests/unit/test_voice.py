"""Keep-voice timing rules (DESIGN_V2 §4, §8)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from c2a.audio.voice import SegmentTime, group_phrases, place
from c2a.providers.base import Word
from c2a.providers.fake import SR, EnergyWordTimer, write_wav

W = Word


def test_group_phrases_by_silence() -> None:
    words = [W(0.4, 0.9, "a"), W(1.1, 1.6, "b"), W(3.3, 3.7, "c"), W(3.9, 4.6, "d")]
    ph = group_phrases(words, 0.3)
    assert [(p.start, p.end, [w.text for w in p.words]) for p in ph] == [
        (0.4, 1.6, ["a", "b"]), (3.3, 4.6, ["c", "d"])]


def test_same_length_clip_keeps_offsets_exactly() -> None:
    ph = group_phrases([W(1.25, 1.8, "a"), W(3.1, 3.5, "b")], 0.3)
    seg = [SegmentTime(0, 1.0, 3.0, 2.0, 10.0), SegmentTime(1, 3.0, 5.0, 2.0, 12.0)]
    p = place(ph, seg)
    assert [(x.segment, x.tempo) for x in p] == [(0, 1.0), (1, 1.0)]
    assert [x.out_start for x in p] == pytest.approx([10.25, 12.1])  # same offset from key


def test_within_tolerance_stretches_offset_and_tempo() -> None:
    ph = group_phrases([W(1.5, 2.0, "a")], 0.3)
    p = place(ph, [SegmentTime(0, 1.0, 3.0, 2.2, 0.0)])[0]  # r = 1.1
    assert p.out_start == pytest.approx(0.55, abs=0.01)  # 0.5 s × 1.1
    assert p.tempo == pytest.approx(1 / 1.1)
    assert p.out_end == pytest.approx(0.55 + 0.5 * 1.1, abs=0.01)


def test_shorter_clip_cuts_at_last_whole_word() -> None:
    ph = group_phrases([W(0.2, 0.6, "a"), W(0.7, 1.1, "b"), W(1.2, 1.9, "c")], 0.3)
    p = place(ph, [SegmentTime(0, 0.0, 4.0, 1.5, 0.0)])[0]  # r = 0.375 → natural speed
    assert p.tempo == 1.0 and p.src_end == 1.1 and p.cut_words == ("c",) and p.fade_out
    assert p.out_end <= 1.5


def test_longer_clip_keeps_natural_speed() -> None:
    ph = group_phrases([W(0.2, 0.6, "a")], 0.3)
    p = place(ph, [SegmentTime(0, 0.0, 1.0, 3.0, 5.0)])[0]  # r = 3
    assert (p.tempo, p.out_start, p.cut_words) == (1.0, 5.2, ())


def test_phrase_after_the_clip_end_is_all_cut_and_phrases_never_cross_clips() -> None:
    ph = group_phrases([W(0.2, 0.6, "a"), W(2.5, 2.9, "late")], 0.3)
    segs = [SegmentTime(0, 0.0, 3.0, 1.0, 0.0), SegmentTime(1, 3.0, 4.0, 1.0, 1.0)]
    p = place(ph, segs)
    late = next(x for x in p if x.cut_words == ("late",))
    assert late.src_end == late.src_start  # nothing played
    for x in p:
        seg = segs[x.segment]
        assert x.out_end <= seg.out_start + seg.clip_s + 1e-9


def test_first_word_longer_than_room_is_faded_not_dropped() -> None:
    ph = group_phrases([W(0.9, 2.5, "long")], 0.3)
    p = place(ph, [SegmentTime(0, 0.0, 4.0, 1.2, 0.0)])[0]
    assert p.fade_out and p.src_end == pytest.approx(0.9 + 0.3) and p.out_end <= 1.2 + 1e-9


def test_energy_vad_finds_bursts_within_20ms(tmp_path: Path) -> None:
    bursts = [(0.4, 0.9), (1.1, 1.6), (3.3, 3.7)]
    t = np.arange(round(5 * SR)) / SR
    sig = np.zeros_like(t)
    for a, b in bursts:
        m = (t >= a) & (t < b)
        sig[m] = 0.3 * np.sin(2 * np.pi * 220 * t[m])
    write_wav(tmp_path / "v.wav", np.stack([sig, sig], 1).astype(np.float32))
    words = EnergyWordTimer().words(tmp_path / "v.wav")
    assert len(words) == 3
    for w, (a, b) in zip(words, bursts, strict=True):
        assert abs(w.start - a) <= 0.02 and abs(w.end - b) <= 0.02
