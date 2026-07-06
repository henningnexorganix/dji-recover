from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from dji_recover import __version__
from dji_recover.cli import main
from dji_recover.hevc import HEVC_AUD, START_CODE, ParameterSets, PpsInfo, SpsInfo, parse_slice_info
from dji_recover.audio import recover_dji_aac_adts
from dji_recover.recover import NalRange, RecoveryError, find_hevc_start, recover_hevc_annexb


def nal(nal_type: int, body: bytes = b"payload") -> bytes:
    header0 = (nal_type << 1) & 0x7E
    header1 = 0x01
    return bytes([header0, header1]) + body


def slice_nal(nal_type: int, first_slice: bool, body: bytes = b"payload") -> bytes:
    flag = b"\x80" if first_slice else b"\x00"
    return nal(nal_type, flag + body)


def length_prefixed(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def bits_to_bytes(bits: str) -> bytes:
    padding = (8 - len(bits) % 8) % 8
    bits += "0" * padding
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8))


def ue_bits(value: int) -> str:
    code_num = value + 1
    binary = format(code_num, "b")
    return "0" * (len(binary) - 1) + binary


class RecoverTests(unittest.TestCase):
    def test_cli_version(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"dji-recover {__version__}")

    def test_cli_rejects_empty_broken_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reference = tmp_path / "reference.mp4"
            broken = tmp_path / "empty.mp4"
            output = tmp_path / "out.mp4"
            reference.write_bytes(b"not empty")
            broken.write_bytes(b"")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(["--reference", str(reference), "--broken", str(broken), "--output", str(output)])

            self.assertEqual(code, 2)
            self.assertIn("Broken file is empty (0 bytes)", stderr.getvalue())

    def test_find_hevc_start_rejects_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "empty.mp4"
            broken.write_bytes(b"")

            with self.assertRaisesRegex(RecoveryError, "empty"):
                find_hevc_start(broken, max_scan=None, max_nal_size=1024)

    def test_recover_resyncs_after_bad_word(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = ParameterSets(vps=nal(32), sps=nal(33), pps=nal(34))
            first = nal(19, b"first")
            second = nal(1, b"second")
            third = nal(1, b"third")
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(
                b"junk"
                + length_prefixed(first)
                + b"\x21\x11\x45\x00BADMETADATA"
                + length_prefixed(second)
                + length_prefixed(third)
            )

            out = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(broken, out, params, start_offset=4, max_nal_size=1024)

            data = out.read_bytes()
            self.assertEqual(stats.nals_written, 3)
            self.assertGreaterEqual(stats.invalid_words, 0)
            self.assertTrue(data.startswith(params.as_annexb()))
            self.assertEqual(data.count(START_CODE), 6)

    def test_find_hevc_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            units = [nal(19, b"one"), nal(1, b"two"), nal(1, b"three"), nal(1, b"four")]
            broken = tmp_path / "broken.mp4"
            prefix = b"x" * 123
            broken.write_bytes(prefix + b"".join(length_prefixed(unit) for unit in units))

            self.assertEqual(find_hevc_start(broken, max_scan=None, max_nal_size=1024), len(prefix))

    def test_find_hevc_start_with_interleaved_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            units = [nal(19, b"one"), nal(1, b"two"), nal(1, b"three"), nal(1, b"four")]
            broken = tmp_path / "broken.mp4"
            prefix = b"x" * 321
            gap = b"\x21\x1b\x94" + (b"a" * 200)
            broken.write_bytes(prefix + gap.join(length_prefixed(unit) for unit in units))

            self.assertEqual(find_hevc_start(broken, max_scan=None, max_nal_size=1024), len(prefix))

    def test_recover_dji_aac_from_video_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = ParameterSets(vps=nal(32), sps=nal(33), pps=nal(34))
            first = nal(19, b"first")
            second = nal(1, b"second")
            audio_frame_a = b"\x21\x1b\x94" + (b"a" * 843)
            audio_frame_b = b"\x21\x1b\x94" + (b"b" * 844)
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(
                length_prefixed(first)
                + audio_frame_a
                + audio_frame_b
                + b"\x1a\x00\x00\x00"
                + length_prefixed(second)
            )

            hevc = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(broken, hevc, params, start_offset=0, max_nal_size=1024)
            audio = tmp_path / "out.aac"
            audio_stats = recover_dji_aac_adts(broken, audio, stats.video_ranges)

            data = audio.read_bytes()
            self.assertEqual(audio_stats.frames_written, 2)
            self.assertEqual(audio_stats.exact_frames, 1)
            self.assertEqual(audio_stats.guessed_last_frames, 1)
            self.assertEqual(data.count(b"\xff\xf1"), 2)
            self.assertIn(b"b" * 844, data)

    def test_recover_dji_aac_can_disable_guessed_gap_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first = nal(19, b"first")
            second = nal(1, b"second")
            audio_frame_a = b"\x21\x1b\x94" + (b"a" * 843)
            audio_frame_b = b"\x21\x1b\x94" + (b"b" * 844)
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(
                length_prefixed(first)
                + audio_frame_a
                + audio_frame_b
                + b"\x1a\x00\x00\x00"
                + length_prefixed(second)
            )

            audio = tmp_path / "out.aac"
            first_end = len(length_prefixed(first))
            second_start = len(broken.read_bytes()) - len(length_prefixed(second))
            second_end = len(broken.read_bytes())
            video_ranges = [
                NalRange(offset=0, payload_start=4, payload_end=first_end, nal_size=len(first), nal_type=19),
                NalRange(
                    offset=second_start,
                    payload_start=second_start + 4,
                    payload_end=second_end,
                    nal_size=len(second),
                    nal_type=1,
                ),
            ]
            audio_stats = recover_dji_aac_adts(broken, audio, video_ranges, guess_final_frames=False)

            data = audio.read_bytes()
            self.assertEqual(audio_stats.frames_written, 1)
            self.assertEqual(audio_stats.exact_frames, 1)
            self.assertEqual(audio_stats.guessed_last_frames, 0)
            self.assertEqual(data.count(b"\xff\xf1"), 1)

    def test_pairs_frame_filter_drops_incomplete_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = ParameterSets(vps=nal(32), sps=nal(33), pps=nal(34))
            complete_a = slice_nal(19, True, b"complete-a")
            complete_b = slice_nal(19, False, b"complete-b")
            orphan_continuation = slice_nal(1, False, b"orphan-continuation")
            orphan_first = slice_nal(1, True, b"orphan-first")
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(
                length_prefixed(complete_a)
                + length_prefixed(complete_b)
                + length_prefixed(orphan_continuation)
                + length_prefixed(orphan_first)
            )

            out = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(
                broken,
                out,
                params,
                start_offset=0,
                max_nal_size=1024,
                frame_filter="pairs",
            )

            data = out.read_bytes()
            self.assertEqual(stats.frames_written, 1)
            self.assertEqual(stats.frames_dropped, 2)
            self.assertEqual(stats.nals_written, 2)
            self.assertIn(b"complete-a", data)
            self.assertIn(b"complete-b", data)
            self.assertNotIn(b"orphan-continuation", data)
            self.assertNotIn(b"orphan-first", data)

    def test_recover_can_insert_hevc_aud_per_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = ParameterSets(vps=nal(32), sps=nal(33), pps=nal(34))
            frame_a = [slice_nal(19, True, b"a1"), slice_nal(19, False, b"a2")]
            frame_b = [slice_nal(1, True, b"b1"), slice_nal(1, False, b"b2")]
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(b"".join(length_prefixed(unit) for unit in frame_a + frame_b))

            out = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(
                broken,
                out,
                params,
                start_offset=0,
                max_nal_size=1024,
                frame_filter="pairs",
                insert_aud=True,
            )

            data = out.read_bytes()
            self.assertEqual(stats.frames_written, 2)
            self.assertEqual(data.count(START_CODE + HEVC_AUD), 2)

    def test_parse_slice_info_reads_poc_and_slice_address(self) -> None:
        sps = SpsInfo(log2_max_pic_order_cnt_lsb=8, slice_segment_address_bits=12)
        pps = PpsInfo(
            pps_id=0,
            sps_id=0,
            dependent_slice_segments_enabled=False,
            output_flag_present=False,
            num_extra_slice_header_bits=0,
        )
        first_payload = nal(1, bits_to_bytes("1" + ue_bits(0) + ue_bits(1) + format(37, "08b")))
        second_payload = nal(
            1,
            bits_to_bytes("0" + ue_bits(0) + format(42, "012b") + ue_bits(1) + format(37, "08b")),
        )

        first = parse_slice_info(first_payload, sps, {0: pps})
        second = parse_slice_info(second_payload, sps, {0: pps})

        self.assertTrue(first.first_slice_segment)
        self.assertEqual(first.slice_segment_address, 0)
        self.assertEqual(first.poc_lsb, 37)
        self.assertFalse(second.first_slice_segment)
        self.assertEqual(second.slice_segment_address, 42)
        self.assertEqual(second.poc_lsb, 37)

    def test_recover_can_skip_to_next_idr_gop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            params = ParameterSets(vps=nal(32), sps=nal(33), pps=nal(34))
            first_gop = [
                slice_nal(19, True, b"first-idr-a"),
                slice_nal(19, False, b"first-idr-b"),
                slice_nal(1, True, b"first-p-a"),
                slice_nal(1, False, b"first-p-b"),
            ]
            second_gop = [
                slice_nal(19, True, b"second-idr-a"),
                slice_nal(19, False, b"second-idr-b"),
            ]
            broken = tmp_path / "broken.mp4"
            broken.write_bytes(b"".join(length_prefixed(unit) for unit in first_gop + second_gop))

            out = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(
                broken,
                out,
                params,
                start_offset=0,
                max_nal_size=1024,
                frame_filter="pairs",
                gop_start="next-idr",
            )

            data = out.read_bytes()
            self.assertEqual(stats.frames_written, 1)
            self.assertNotIn(b"first-idr-a", data)
            self.assertNotIn(b"first-p-a", data)
            self.assertIn(b"second-idr-a", data)


if __name__ == "__main__":
    unittest.main()
