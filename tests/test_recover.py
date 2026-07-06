from pathlib import Path
import tempfile
import unittest

from dji_recover.hevc import START_CODE, ParameterSets
from dji_recover.audio import recover_dji_aac_adts
from dji_recover.recover import find_hevc_start, recover_hevc_annexb


def nal(nal_type: int, body: bytes = b"payload") -> bytes:
    header0 = (nal_type << 1) & 0x7E
    header1 = 0x01
    return bytes([header0, header1]) + body


def length_prefixed(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


class RecoverTests(unittest.TestCase):
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
                + b"\x00\x00\x00\x00"
                + length_prefixed(second)
            )

            hevc = tmp_path / "out.hevc"
            stats = recover_hevc_annexb(broken, hevc, params, start_offset=0, max_nal_size=1024)
            audio = tmp_path / "out.aac"
            audio_stats = recover_dji_aac_adts(broken, audio, stats.video_ranges)

            data = audio.read_bytes()
            self.assertEqual(audio_stats.frames_written, 2)
            self.assertEqual(data.count(b"\xff\xf1"), 2)


if __name__ == "__main__":
    unittest.main()
