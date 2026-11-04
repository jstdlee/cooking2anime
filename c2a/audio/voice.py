"""Keep-voice timing (DESIGN_V2 §4): pure functions, no I/O.

1. Words → phrases (split where silence ≥ phrase_gap_s).
2. A phrase belongs to the segment where it *starts*.
3. In clip i it starts at the same offset from the key image as in the source.
4. Fit: r = clip_len / seg_len.
   - |r - 1| ≤ tol: stretch the phrase by r (pitch kept), offsets scale too.
   - r < 1 - tol: natural speed; cut after the last whole word that ends before the clip end.
   - r > 1 + tol: natural speed; the rest of the clip is silence/room tone.
5. A phrase never crosses into the next clip.
"""

from __future__ import annotations

from dataclasses import dataclass

from c2a.providers.base import Word


@dataclass(frozen=True)
class Phrase:
    start: float
    end: float
    words: tuple[Word, ...]


@dataclass(frozen=True)
class SegmentTime:
    index: int
    src_start: float  # seconds in the source
    src_end: float
    clip_s: float  # generated clip length
    out_start: float  # clip start on the output timeline


@dataclass(frozen=True)
class Placement:
    segment: int
    src_start: float  # what to take from the voice stem (seconds)
    src_end: float
    out_start: float  # where it lands on the output timeline
    tempo: float  # >1 plays faster (ffmpeg atempo); 1.0 = natural speed
    cut_words: tuple[str, ...]  # words that did not fit
    fade_out: bool

    @property
    def out_end(self) -> float:
        return self.out_start + (self.src_end - self.src_start) / self.tempo


def group_phrases(words: list[Word], gap_s: float) -> list[Phrase]:
    phrases: list[Phrase] = []
    cur: list[Word] = []
    for w in sorted(words, key=lambda w: w.start):
        if cur and w.start - cur[-1].end >= gap_s:
            phrases.append(Phrase(cur[0].start, cur[-1].end, tuple(cur)))
            cur = []
        cur.append(w)
    if cur:
        phrases.append(Phrase(cur[0].start, cur[-1].end, tuple(cur)))
    return phrases


def place(phrases: list[Phrase], segments: list[SegmentTime],
          tolerance: float = 0.15) -> list[Placement]:
    out: list[Placement] = []
    for seg in segments:
        seg_len = seg.src_end - seg.src_start
        if seg_len <= 0:
            continue
        r = seg.clip_s / seg_len
        stretch = abs(r - 1) <= tolerance
        tempo = 1 / r if stretch else 1.0
        for ph in phrases:
            if not seg.src_start <= ph.start < seg.src_end:
                continue
            offset = (ph.start - seg.src_start) * (r if stretch else 1.0)
            if offset >= seg.clip_s:
                out.append(Placement(seg.index, ph.start, ph.start, seg.out_start + seg.clip_s,
                                     tempo, tuple(w.text for w in ph.words), False))
                continue
            room = seg.clip_s - offset  # output seconds available in this clip
            kept: list[Word] = []
            for w in ph.words:
                if (w.end - ph.start) / tempo <= room + 1e-9:
                    kept.append(w)
                else:
                    break
            cut = tuple(w.text for w in ph.words[len(kept):])
            if not kept:  # not even the first word fits: keep what fits and fade it out
                src_end = ph.start + room * tempo
                fade = True
            else:
                src_end = kept[-1].end
                fade = bool(cut)
            out.append(Placement(seg.index, ph.start, src_end, seg.out_start + offset, tempo,
                                 cut, fade))
    return out
