from __future__ import annotations

from dataclasses import dataclass, field
import mmap
from pathlib import Path

from .hevc import PARAMETER_SET_TYPES, START_CODE, ParameterSets, hevc_nal_type, looks_like_hevc_nal

DJI_HEVC_TYPES = {1, 19, 20, 32, 33, 34}
DJI_SLICE_HEADERS = {b"\x02\x01", b"\x26\x01", b"\x28\x01"}


@dataclass
class NalRange:
    offset: int
    payload_start: int
    payload_end: int
    nal_size: int
    nal_type: int


@dataclass
class RecoveryStats:
    start_offset: int
    bytes_scanned: int = 0
    nals_written: int = 0
    frames_written: int = 0
    frames_dropped: int = 0
    parameter_sets_skipped: int = 0
    resyncs: int = 0
    invalid_words: int = 0
    side_tracks_skipped: int = 0
    largest_nal: int = 0
    last_output_offset: int = 0
    video_ranges: list[NalRange] = field(default_factory=list)


def parse_offset(value: str | int | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    return int(value, 0)


def find_hevc_start(path: Path, max_scan: int | None, max_nal_size: int) -> int:
    size = path.stat().st_size
    limit = min(size, max_scan) if max_scan else size
    indexed_start = _find_indexed_hevc_start(path, limit, max_nal_size, min_count=4)
    if indexed_start is not None:
        return indexed_start

    with path.open("rb") as handle:
        window = bytearray()
        window_offset = 0
        read_offset = 0
        chunk_size = 8 * 1024 * 1024
        keep = max(1024 * 1024, min(max_nal_size * 4 + 64, 64 * 1024 * 1024))
        while read_offset < limit:
            to_read = min(chunk_size, limit - read_offset)
            chunk = handle.read(to_read)
            if not chunk:
                break
            window.extend(chunk)

            search_limit = max(0, len(window) - 16)
            for i in range(search_limit):
                if _valid_chain_in_buffer(window, i, max_nal_size, min_count=4):
                    return window_offset + i

            read_offset += len(chunk)
            if len(window) > keep:
                drop = len(window) - keep
                window_offset += drop
                del window[:drop]
    raise RuntimeError("Could not find a plausible HEVC length-prefixed stream in the broken file")


def _find_indexed_hevc_start(path: Path, limit: int, max_nal_size: int, min_count: int) -> int | None:
    max_gap = max(2 * 1024 * 1024, max_nal_size * 4)
    with path.open("rb") as handle:
        data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            search_limit = min(limit, len(data))
            pos = 4
            cluster_start: int | None = None
            cluster_count = 0
            previous_end: int | None = None

            while pos < search_limit - 6:
                hit = _find_next_slice_header(data, pos, end=search_limit)
                if hit is None:
                    break
                candidate = hit - 4
                pos = hit + 1

                nal_range = _valid_dji_nal_range(data, candidate, max_nal_size)
                if nal_range is None:
                    continue

                if previous_end is None or candidate < previous_end or candidate - previous_end > max_gap:
                    cluster_start = candidate
                    cluster_count = 1
                else:
                    cluster_count += 1

                previous_end = nal_range.payload_end
                if cluster_start is not None and cluster_count >= min_count:
                    return cluster_start
        finally:
            data.close()

    return None


def recover_hevc_annexb(
    broken: Path,
    output_hevc: Path,
    parameter_sets: ParameterSets,
    start_offset: int | None = None,
    max_scan: int | None = None,
    max_nal_size: int = 512 * 1024,
    frame_filter: str = "none",
) -> RecoveryStats:
    if start_offset is None:
        start_offset = find_hevc_start(broken, max_scan=max_scan, max_nal_size=max_nal_size)

    stats = RecoveryStats(start_offset=start_offset)
    return _recover_hevc_annexb_indexed(
        broken,
        output_hevc,
        parameter_sets,
        start_offset,
        max_nal_size,
        stats,
        frame_filter,
    )


def _recover_hevc_annexb_indexed(
    broken: Path,
    output_hevc: Path,
    parameter_sets: ParameterSets,
    start_offset: int,
    max_nal_size: int,
    stats: RecoveryStats,
    frame_filter: str,
) -> RecoveryStats:
    file_size = broken.stat().st_size
    last_end = start_offset
    frame: list[tuple[NalRange, bytes]] = []
    with broken.open("rb") as src, output_hevc.open("wb") as dst:
        data = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            dst.write(parameter_sets.as_annexb())

            def flush_frame() -> None:
                nonlocal frame
                if not frame:
                    return
                if _keep_frame(frame, frame_filter):
                    _write_frame(dst, frame, stats)
                else:
                    stats.frames_dropped += 1
                frame = []

            pos = start_offset + 4
            while pos < file_size - 6:
                hit = _find_next_slice_header(data, pos)
                if hit is None:
                    break
                candidate = hit - 4
                pos = hit + 1
                if candidate < start_offset or candidate < last_end:
                    continue

                nal_range = _valid_dji_nal_range(data, candidate, max_nal_size)
                if nal_range is None:
                    stats.invalid_words += 1
                    continue
                nal_size = nal_range.nal_size
                payload_end = nal_range.payload_end
                payload = data[nal_range.payload_start:payload_end]
                nal_type = nal_range.nal_type
                if nal_type in PARAMETER_SET_TYPES:
                    stats.parameter_sets_skipped += 1
                    last_end = payload_end
                    continue

                stats.bytes_scanned = candidate - start_offset
                if frame_filter == "none":
                    _write_frame(dst, [(nal_range, payload)], stats)
                else:
                    first_slice = _first_slice_segment(payload)
                    if first_slice:
                        flush_frame()
                        frame.append((nal_range, payload))
                    elif frame:
                        if frame_filter == "pairs" and len(frame) >= 2:
                            flush_frame()
                            stats.frames_dropped += 1
                        else:
                            frame.append((nal_range, payload))
                    else:
                        stats.frames_dropped += 1
                last_end = payload_end
            flush_frame()
        finally:
            data.close()

    return stats


def _write_frame(dst, frame: list[tuple[NalRange, bytes]], stats: RecoveryStats) -> None:
    for nal_range, payload in frame:
        dst.write(START_CODE)
        dst.write(payload)
        stats.nals_written += 1
        stats.largest_nal = max(stats.largest_nal, nal_range.nal_size)
        stats.last_output_offset = nal_range.offset
        stats.video_ranges.append(nal_range)
    stats.frames_written += 1


def _keep_frame(frame: list[tuple[NalRange, bytes]], frame_filter: str) -> bool:
    if frame_filter == "none":
        return True
    if not frame or not _first_slice_segment(frame[0][1]):
        return False
    if any(_first_slice_segment(payload) for _, payload in frame[1:]):
        return False
    if frame_filter == "pairs":
        return len(frame) == 2
    if frame_filter == "complete":
        return True
    raise ValueError(f"Unknown frame filter: {frame_filter}")


def _first_slice_segment(payload: bytes) -> bool:
    return len(payload) > 2 and bool(payload[2] & 0x80)


def _recover_hevc_annexb_online(
    broken: Path,
    output_hevc: Path,
    parameter_sets: ParameterSets,
    start_offset: int,
    max_nal_size: int,
    stats: RecoveryStats,
) -> RecoveryStats:
    file_size = broken.stat().st_size
    with broken.open("rb") as src, output_hevc.open("wb") as dst:
        src.seek(start_offset)
        dst.write(parameter_sets.as_annexb())

        while True:
            pos = src.tell()
            raw_size = src.read(4)
            if len(raw_size) < 4:
                break
            nal_size = int.from_bytes(raw_size, "big")
            stats.bytes_scanned = pos - start_offset

            if not (0 < nal_size <= max_nal_size) or pos + 4 + nal_size > file_size:
                if _skip_side_track(src, pos, nal_size):
                    stats.side_tracks_skipped += 1
                    continue
                stats.invalid_words += 1
                resynced = _resync(src, file_size, max_nal_size)
                if not resynced:
                    break
                stats.resyncs += 1
                continue

            header = src.read(2)
            if len(header) < 2:
                break
            if not _looks_like_dji_hevc_nal(header):
                src.seek(pos + 4)
                if _skip_side_track(src, pos, nal_size):
                    stats.side_tracks_skipped += 1
                    continue
                stats.invalid_words += 1
                src.seek(pos + 1)
                resynced = _resync(src, file_size, max_nal_size)
                if not resynced:
                    break
                stats.resyncs += 1
                continue

            rest = src.read(nal_size - 2)
            if len(rest) < nal_size - 2:
                break
            payload = header + rest
            nal_type = hevc_nal_type(payload)
            if _contains_start_code(payload):
                stats.invalid_words += 1
                src.seek(pos + 1)
                resynced = _resync(src, file_size, max_nal_size)
                if not resynced:
                    break
                stats.resyncs += 1
                continue

            if nal_type in PARAMETER_SET_TYPES:
                stats.parameter_sets_skipped += 1
                continue

            dst.write(START_CODE)
            dst.write(payload)
            stats.nals_written += 1
            stats.largest_nal = max(stats.largest_nal, nal_size)
            stats.last_output_offset = pos

    return stats


def _find_next_slice_header(data: mmap.mmap, start: int, end: int | None = None) -> int | None:
    if end is None:
        hits = [idx for header in DJI_SLICE_HEADERS if (idx := data.find(header, start)) != -1]
    else:
        hits = [idx for header in DJI_SLICE_HEADERS if (idx := data.find(header, start, end)) != -1]
    return min(hits) if hits else None


def _valid_dji_nal_range(data: mmap.mmap, candidate: int, max_nal_size: int) -> NalRange | None:
    if candidate < 0 or candidate + 6 > len(data):
        return None
    payload_start = candidate + 4
    nal_size = int.from_bytes(data[candidate:payload_start], "big")
    payload_end = payload_start + nal_size
    if not (0 < nal_size <= max_nal_size) or payload_end > len(data):
        return None

    payload = data[payload_start:payload_end]
    if not _looks_like_dji_hevc_nal(payload[:2]) or _contains_start_code(payload):
        return None

    return NalRange(
        offset=candidate,
        payload_start=payload_start,
        payload_end=payload_end,
        nal_size=nal_size,
        nal_type=hevc_nal_type(payload),
    )


def _valid_chain_in_buffer(buffer: bytes | bytearray, offset: int, max_nal_size: int, min_count: int) -> bool:
    pos = offset
    for _ in range(min_count):
        if pos + 6 > len(buffer):
            return False
        nal_size = int.from_bytes(buffer[pos : pos + 4], "big")
        if not (0 < nal_size <= max_nal_size):
            return False
        payload_start = pos + 4
        payload_end = payload_start + nal_size
        if payload_end > len(buffer):
            return False
        payload = buffer[payload_start:payload_end]
        if not _looks_like_dji_hevc_nal(payload[:2]) or _contains_start_code(payload):
            return False
        pos = payload_end
    return True


def _skip_side_track(src, word_pos: int, word: int) -> bool:
    src.seek(word_pos + 4)

    if (word & 0xFFFF0000) in {0x211B0000, 0x212B0000, 0x214D0000, 0x217B0000}:
        return _scan_to_word(src, _is_metadata_marker)

    if (word & 0xFFFE0000) == 0x1A2E0000:
        size = 0x30 + (1 if word & 0x00010000 else 0)
        src.seek(word_pos + size)
        return True

    if (word & 0xFFFF0000) == 0x1A2D0000:
        size = 0x2F + (word & 0x0000FFF0) - 0x0A00
        if size > 4:
            src.seek(word_pos + size)
            return True

    if (word & 0xFFF00000) == 0x1A700000:
        size = 0x79 + (word >> 16) - 0x1A77
        if size > 4:
            src.seek(word_pos + size)
            return True

    if (word & 0xFF800000) == 0x1A800000:
        lower = word & 0xFF80FFFF
        if lower == 0x1A80010A:
            size = 0x83 + (word >> 16) - 0x1A80
        elif lower == 0x1A80020A:
            size = 0x103 + (word >> 16) - 0x1A80
        elif lower == 0x1A80030A:
            size = 0x183 + (word >> 16) - 0x1A80
        elif lower == 0x1A80040A:
            size = 0x203 + (word >> 16) - 0x1A80
        elif lower == 0x1A80070A:
            size = 0x303 + (word >> 16) - 0x1A00
        elif lower == 0x1A80080A:
            size = 0x403 + (word >> 16) - 0x1A80
        elif lower == 0x1A800D0A:
            size = 0x603 + (word >> 16) - 0x1A00
        elif lower == 0x1A800E0A:
            size = 0x703 + (word >> 16) - 0x1A80
        else:
            size = (word >> 16) - 0x177D
        if size > 4:
            src.seek(word_pos + size)
            return True

    return False


def _is_metadata_marker(word: int) -> bool:
    return (
        (word & 0xFFFCFFF0) == 0x1A2C0A00
        or (word & 0xFFF0FFF0) == 0x1A700A00
        or (word & 0xFF80FFFF) == 0x1A80020A
    )


def _scan_to_word(src, predicate, max_scan: int = 8 * 1024 * 1024) -> bool:
    start = src.tell()
    raw = src.read(4)
    if len(raw) < 4:
        return False
    word = int.from_bytes(raw, "big")
    scanned = 4
    while scanned <= max_scan:
        if predicate(word):
            src.seek(src.tell() - 4)
            return True
        next_byte = src.read(1)
        if not next_byte:
            return False
        word = ((word << 8) & 0xFFFFFFFF) | next_byte[0]
        scanned += 1
    src.seek(start)
    return False


def _resync(src, file_size: int, max_nal_size: int) -> bool:
    start = max(src.tell() - 3, 0)
    src.seek(start)
    chunk_size = 8 * 1024 * 1024
    overlap = max_nal_size + 8
    buffer = bytearray()
    buffer_offset = start

    while buffer_offset < file_size:
        if not buffer:
            src.seek(buffer_offset)
            chunk = src.read(min(chunk_size, file_size - buffer_offset))
            if not chunk:
                return False
            buffer.extend(chunk)

        best_idx = None
        for header in DJI_SLICE_HEADERS:
            idx = buffer.find(header)
            if idx >= 4 and (best_idx is None or idx < best_idx):
                best_idx = idx

        if best_idx is not None:
            candidate = buffer_offset + best_idx - 4
            nal_size = int.from_bytes(buffer[best_idx - 4 : best_idx], "big")
            if 0 < nal_size <= max_nal_size and candidate + 4 + nal_size <= file_size:
                src.seek(candidate)
                return True
            del buffer[: best_idx + 1]
            buffer_offset += best_idx + 1
            continue

        if len(buffer) > overlap:
            drop = len(buffer) - overlap
            buffer_offset += drop
            del buffer[:drop]
        else:
            src.seek(buffer_offset + len(buffer))
            chunk = src.read(min(chunk_size, file_size - (buffer_offset + len(buffer))))
            if not chunk:
                return False
            buffer.extend(chunk)
    return False


def _looks_like_dji_hevc_nal(payload: bytes | bytearray) -> bool:
    nal_type = hevc_nal_type(payload)
    if nal_type not in DJI_HEVC_TYPES or not looks_like_hevc_nal(payload):
        return False
    if nal_type in {1, 19, 20}:
        return bytes(payload[:2]) in DJI_SLICE_HEADERS
    return True


def _contains_start_code(payload: bytes) -> bool:
    return b"\x00\x00\x01" in payload
