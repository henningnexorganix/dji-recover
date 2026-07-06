from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess

START_CODE = b"\x00\x00\x00\x01"
HEVC_AUD = b"\x46\x01\x50"
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


@dataclass(frozen=True)
class SpsInfo:
    log2_max_pic_order_cnt_lsb: int
    slice_segment_address_bits: int


@dataclass(frozen=True)
class PpsInfo:
    pps_id: int
    sps_id: int
    dependent_slice_segments_enabled: bool
    output_flag_present: bool
    num_extra_slice_header_bits: int


@dataclass(frozen=True)
class SliceInfo:
    first_slice_segment: bool
    pps_id: int
    slice_segment_address: int
    poc_lsb: int | None
    slice_type: int | None


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


def parse_sps_info(payload: bytes) -> SpsInfo:
    reader = BitReader(_rbsp_from_ebsp(payload[2:]))
    reader.read_bits(4)
    max_sub_layers_minus1 = reader.read_bits(3)
    reader.read_bits(1)
    _skip_profile_tier_level(reader, max_sub_layers_minus1)
    reader.read_ue()
    chroma_format_idc = reader.read_ue()
    if chroma_format_idc == 3:
        reader.read_bits(1)
    width = reader.read_ue()
    height = reader.read_ue()
    if reader.read_bool():
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()
    reader.read_ue()
    reader.read_ue()
    log2_max_pic_order_cnt_lsb = reader.read_ue() + 4

    sub_layer_ordering_info_present = reader.read_bool()
    start_layer = 0 if sub_layer_ordering_info_present else max_sub_layers_minus1
    for _ in range(start_layer, max_sub_layers_minus1 + 1):
        reader.read_ue()
        reader.read_ue()
        reader.read_ue()

    log2_min_luma_coding_block_size = reader.read_ue() + 3
    log2_diff_max_min_luma_coding_block_size = reader.read_ue()
    ctb_size = 1 << (log2_min_luma_coding_block_size + log2_diff_max_min_luma_coding_block_size)
    pic_width_in_ctbs = math.ceil(width / ctb_size)
    pic_height_in_ctbs = math.ceil(height / ctb_size)
    pic_size_in_ctbs = max(1, pic_width_in_ctbs * pic_height_in_ctbs)
    return SpsInfo(
        log2_max_pic_order_cnt_lsb=log2_max_pic_order_cnt_lsb,
        slice_segment_address_bits=max(1, math.ceil(math.log2(pic_size_in_ctbs))),
    )


def parse_pps_info(payload: bytes) -> PpsInfo:
    reader = BitReader(_rbsp_from_ebsp(payload[2:]))
    pps_id = reader.read_ue()
    sps_id = reader.read_ue()
    dependent_slice_segments_enabled = reader.read_bool()
    output_flag_present = reader.read_bool()
    num_extra_slice_header_bits = reader.read_bits(3)
    return PpsInfo(
        pps_id=pps_id,
        sps_id=sps_id,
        dependent_slice_segments_enabled=dependent_slice_segments_enabled,
        output_flag_present=output_flag_present,
        num_extra_slice_header_bits=num_extra_slice_header_bits,
    )


def parse_slice_info(payload: bytes, sps: SpsInfo, pps_by_id: dict[int, PpsInfo]) -> SliceInfo:
    nal_type = hevc_nal_type(payload)
    if nal_type is None or nal_type not in set(range(0, 32)):
        raise ValueError("Not a VCL HEVC NAL unit")

    reader = BitReader(_rbsp_from_ebsp(payload[2:]))
    first_slice_segment = reader.read_bool()
    if 16 <= nal_type <= 23:
        reader.read_bits(1)
    pps_id = reader.read_ue()
    pps = pps_by_id[pps_id]

    dependent_slice_segment = False
    slice_segment_address = 0
    if not first_slice_segment:
        if pps.dependent_slice_segments_enabled:
            dependent_slice_segment = reader.read_bool()
        slice_segment_address = reader.read_bits(sps.slice_segment_address_bits)
    if dependent_slice_segment:
        return SliceInfo(first_slice_segment, pps_id, slice_segment_address, None, None)

    if pps.num_extra_slice_header_bits:
        reader.read_bits(pps.num_extra_slice_header_bits)
    slice_type = reader.read_ue()
    if pps.output_flag_present:
        reader.read_bits(1)

    poc_lsb = None
    if nal_type not in {19, 20}:
        poc_lsb = reader.read_bits(sps.log2_max_pic_order_cnt_lsb)

    return SliceInfo(first_slice_segment, pps_id, slice_segment_address, poc_lsb, slice_type)


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


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_pos = 0

    def read_bool(self) -> bool:
        return bool(self.read_bits(1))

    def read_bits(self, count: int) -> int:
        if count < 0:
            raise ValueError("Cannot read a negative number of bits")
        value = 0
        for _ in range(count):
            byte_pos = self.bit_pos // 8
            if byte_pos >= len(self.data):
                raise ValueError("Unexpected end of HEVC bitstream")
            bit_offset = 7 - (self.bit_pos % 8)
            value = (value << 1) | ((self.data[byte_pos] >> bit_offset) & 1)
            self.bit_pos += 1
        return value

    def read_ue(self) -> int:
        zeros = 0
        while self.read_bits(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("Exp-Golomb code is too large")
        suffix = self.read_bits(zeros) if zeros else 0
        return (1 << zeros) - 1 + suffix


def _rbsp_from_ebsp(data: bytes) -> bytes:
    rbsp = bytearray()
    zero_count = 0
    for byte in data:
        if zero_count >= 2 and byte == 0x03:
            zero_count = 0
            continue
        rbsp.append(byte)
        if byte == 0:
            zero_count += 1
        else:
            zero_count = 0
    return bytes(rbsp)


def _skip_profile_tier_level(reader: BitReader, max_sub_layers_minus1: int) -> None:
    reader.read_bits(96)
    sub_layer_profile_present = []
    sub_layer_level_present = []
    for _ in range(max_sub_layers_minus1):
        sub_layer_profile_present.append(reader.read_bool())
        sub_layer_level_present.append(reader.read_bool())
    if max_sub_layers_minus1 > 0:
        for _ in range(8 - max_sub_layers_minus1):
            reader.read_bits(2)
    for profile_present, level_present in zip(sub_layer_profile_present, sub_layer_level_present):
        if profile_present:
            reader.read_bits(88)
        if level_present:
            reader.read_bits(8)


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
