from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

START_CODE = b"\x00\x00\x00\x01"
PARAMETER_SET_TYPES = {32, 33, 34}
COMMON_HEVC_TYPES = set(range(0, 32)) | {32, 33, 34, 35, 39, 40}


@dataclass(frozen=True)
class NalUnit:
    nal_type: int
    payload: bytes


@dataclass(frozen=True)
class ParameterSets:
    vps: bytes
    sps: bytes
    pps: bytes

    def as_annexb(self) -> bytes:
        return START_CODE + self.vps + START_CODE + self.sps + START_CODE + self.pps


def hevc_nal_type(payload: bytes) -> int | None:
    if len(payload) < 2:
        return None
    if payload[0] & 0x80:
        return None
    if payload[1] & 0x07 == 0:
        return None
    nal_type = (payload[0] >> 1) & 0x3F
    if nal_type > 40:
        return None
    return nal_type


def looks_like_hevc_nal(payload: bytes) -> bool:
    nal_type = hevc_nal_type(payload)
    return nal_type in COMMON_HEVC_TYPES if nal_type is not None else False


def split_annexb(data: bytes) -> list[NalUnit]:
    starts: list[tuple[int, int]] = []
    i = 0
    while i < len(data) - 3:
        if data[i : i + 4] == START_CODE:
            starts.append((i, 4))
            i += 4
        elif data[i : i + 3] == b"\x00\x00\x01":
            starts.append((i, 3))
            i += 3
        else:
            i += 1

    units: list[NalUnit] = []
    for idx, (pos, code_len) in enumerate(starts):
        payload_start = pos + code_len
        payload_end = starts[idx + 1][0] if idx + 1 < len(starts) else len(data)
        payload = data[payload_start:payload_end].strip(b"\x00")
        nal_type = hevc_nal_type(payload)
        if payload and nal_type is not None:
            units.append(NalUnit(nal_type=nal_type, payload=payload))
    return units


def extract_parameter_sets(reference: Path, workdir: Path, seconds: int = 2) -> ParameterSets:
    annexb_path = workdir / "reference.hevc"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(reference),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-t",
        str(seconds),
        str(annexb_path),
    ]
    subprocess.run(cmd, check=True)

    units = split_annexb(annexb_path.read_bytes())
    found: dict[int, bytes] = {}
    for unit in units:
        if unit.nal_type in PARAMETER_SET_TYPES and unit.nal_type not in found:
            found[unit.nal_type] = unit.payload
    missing = PARAMETER_SET_TYPES - set(found)
    if missing:
        names = {32: "VPS", 33: "SPS", 34: "PPS"}
        raise RuntimeError("Reference stream is missing " + ", ".join(names[t] for t in sorted(missing)))
    return ParameterSets(vps=found[32], sps=found[33], pps=found[34])
