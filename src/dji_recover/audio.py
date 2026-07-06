from __future__ import annotations

from dataclasses import dataclass
import mmap
from pathlib import Path

from .recover import NalRange

AAC_LC_PROFILE = 2
AAC_SAMPLE_RATE_INDEX = {
    96000: 0,
    88200: 1,
    64000: 2,
    48000: 3,
    44100: 4,
    32000: 5,
    24000: 6,
    22050: 7,
    16000: 8,
    12000: 9,
    11025: 10,
    8000: 11,
    7350: 12,
}
DJI_AAC_SIGNATURE = b"\x21\x1b\x94"
DJI_AAC_FRAME_SIZES = (846, 847)
DJI_BLOCK_MARKER_BYTES = {0x1A, 0x21}


@dataclass
class AudioStats:
    frames_written: int = 0
    regions_scanned: int = 0
    candidate_signatures: int = 0
    guessed_last_frames: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.frames_written * 1024 / 48000

    @property
    def exact_frames(self) -> int:
        return self.frames_written - self.guessed_last_frames

    @property
    def guessed_ratio(self) -> float:
        if self.frames_written == 0:
            return 0.0
        return self.guessed_last_frames / self.frames_written


def recover_dji_aac_adts(
    broken: Path,
    output_adts: Path,
    video_ranges: list[NalRange],
    sample_rate: int = 48000,
    channels: int = 2,
    guess_final_frames: bool = True,
) -> AudioStats:
    if not video_ranges:
        return AudioStats()

    if sample_rate not in AAC_SAMPLE_RATE_INDEX:
        raise ValueError(f"Unsupported AAC sample rate for ADTS output: {sample_rate}")
    if not 1 <= channels <= 7:
        raise ValueError(f"Unsupported AAC channel count for ADTS output: {channels}")

    stats = AudioStats()
    with broken.open("rb") as src, output_adts.open("wb") as dst:
        data = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for previous, current in zip(video_ranges, video_ranges[1:]):
                gap_start = previous.payload_end
                gap_end = current.offset
                if gap_end <= gap_start:
                    continue

                signatures = _find_signatures(data, gap_start, gap_end)
                if not signatures:
                    continue

                stats.regions_scanned += 1
                stats.candidate_signatures += len(signatures)
                frame_ranges = _aac_frame_ranges(
                    data,
                    signatures,
                    gap_end,
                    guess_final_frames=guess_final_frames,
                )
                for start, end, guessed in frame_ranges:
                    payload = data[start:end]
                    if not payload.startswith(DJI_AAC_SIGNATURE):
                        continue
                    dst.write(_adts_header(len(payload), sample_rate, channels))
                    dst.write(payload)
                    stats.frames_written += 1
                    if guessed:
                        stats.guessed_last_frames += 1
        finally:
            data.close()

    if stats.frames_written == 0:
        output_adts.unlink(missing_ok=True)
    return stats


def _find_signatures(data: mmap.mmap, start: int, end: int) -> list[int]:
    signatures: list[int] = []
    pos = start
    while pos < end:
        hit = data.find(DJI_AAC_SIGNATURE, pos, end)
        if hit == -1:
            break
        signatures.append(hit)
        pos = hit + 1
    return signatures


def _aac_frame_ranges(
    data: mmap.mmap | bytes,
    signatures: list[int],
    region_end: int,
    guess_final_frames: bool = True,
) -> list[tuple[int, int, bool]]:
    ranges: list[tuple[int, int, bool]] = []
    for start, next_start in zip(signatures, signatures[1:]):
        size = next_start - start
        if size in DJI_AAC_FRAME_SIZES:
            ranges.append((start, next_start, False))

    if not guess_final_frames:
        return ranges

    last = signatures[-1]
    guessed_size = _guess_final_frame_size(data, last, region_end)
    if guessed_size is not None:
        ranges.append((last, last + guessed_size, True))
    return ranges


def _guess_final_frame_size(data: mmap.mmap | bytes, start: int, region_end: int) -> int | None:
    space = region_end - start
    for size in reversed(DJI_AAC_FRAME_SIZES):
        marker_pos = start + size
        if space > size and marker_pos < len(data) and data[marker_pos] in DJI_BLOCK_MARKER_BYTES:
            return size

    for size in reversed(DJI_AAC_FRAME_SIZES):
        if space == size:
            return size

    for size in DJI_AAC_FRAME_SIZES:
        if space >= size:
            return size
    return None


def _adts_header(payload_size: int, sample_rate: int, channels: int) -> bytes:
    frame_length = payload_size + 7
    frequency_index = AAC_SAMPLE_RATE_INDEX[sample_rate]
    profile = AAC_LC_PROFILE - 1
    return bytes(
        [
            0xFF,
            0xF1,
            ((profile & 0x03) << 6) | ((frequency_index & 0x0F) << 2) | ((channels >> 2) & 0x01),
            ((channels & 0x03) << 6) | ((frame_length >> 11) & 0x03),
            (frame_length >> 3) & 0xFF,
            ((frame_length & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
