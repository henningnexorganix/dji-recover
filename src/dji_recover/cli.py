from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import tempfile

from . import __version__
from .audio import recover_dji_aac_adts
from .ffmpeg import ffprobe_json, mux_hevc_to_mp4, transcode_audio_to_m4a, try_extract_audio
from .hevc import extract_parameter_sets
from .quality import inspect_gops
from .recover import RecoveryError, parse_offset, recover_hevc_annexb


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dji-recover",
        description="Recover HEVC video from DJI MP4 files with a missing moov atom.",
    )
    parser.add_argument("--version", action="version", version=f"dji-recover {__version__}")
    parser.add_argument("--reference", required=True, type=Path, help="Known-good MP4 from the same camera/settings")
    parser.add_argument("--broken", required=True, type=Path, help="Broken MP4 file to recover")
    parser.add_argument("--output", required=True, type=Path, help="Recovered MP4 output path")
    parser.add_argument("--start-offset", help="Known HEVC payload offset, decimal or hex, e.g. 0x267a")
    parser.add_argument("--frame-rate", default="24000/1001", help="Frame rate for raw HEVC timestamps")
    parser.add_argument(
        "--timeline",
        choices=["preserve", "clean"],
        default="preserve",
        help="preserve keeps recovered timing; clean re-encodes for playback/editing",
    )
    parser.add_argument(
        "--mode",
        choices=["copy", "reencode"],
        help="Video mode. Defaults to HEVC copy for preserve and H.264 reencode for clean.",
    )
    parser.add_argument(
        "--frame-filter",
        choices=["auto", "none", "complete", "pairs", "header-pairs"],
        default="auto",
        help="Filter recovered HEVC access units. auto uses pairs for clean and none for preserve.",
    )
    parser.add_argument(
        "--hevc-aud",
        choices=["auto", "on", "off"],
        default="auto",
        help="Insert HEVC access unit delimiters. auto enables them for clean output.",
    )
    parser.add_argument(
        "--gop-start",
        choices=["first", "next-idr"],
        default="first",
        help="Start recovered video at the first detected GOP or skip to the next IDR GOP.",
    )
    parser.add_argument(
        "--audio",
        choices=["auto", "none"],
        default="auto",
        help="Recover/mux audio when possible, or disable audio.",
    )
    parser.add_argument(
        "--audio-mode",
        choices=["transcode", "copy"],
        default="transcode",
        help="Transcode recovered AAC for compatibility or mux it directly.",
    )
    parser.add_argument(
        "--audio-recovery",
        choices=["guess", "exact"],
        default="guess",
        help="guess includes plausible final AAC frames in DJI video gaps; exact keeps only size-confirmed frames.",
    )
    parser.add_argument(
        "--audio-sync",
        choices=["pad", "shortest"],
        default="pad",
        help="pad extends recovered audio with silence to video length; shortest trims output to the shorter stream.",
    )
    parser.add_argument("--audio-source", type=Path, help="Optional AAC/M4A/WAV file to mux into the recovered MP4")
    parser.add_argument("--keep-workdir", type=Path, help="Keep intermediate files in this directory")
    parser.add_argument("--gop-report", type=Path, help="Write a JSON quality report for recovered keyframes/GOP starts")
    parser.add_argument("--gop-samples-dir", type=Path, help="Directory for JPEG samples referenced by --gop-report")
    parser.add_argument(
        "--gop-report-limit",
        type=int,
        default=200,
        help="Maximum keyframes to inspect for --gop-report; use 0 for all",
    )
    parser.add_argument("--max-scan", default=None, help="Auto-detect scan limit in bytes, decimal or hex")
    parser.add_argument("--max-nal-size", default="0x80000", help="Maximum plausible NAL size")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"dji-recover {__version__}", file=sys.stderr)
    reference = args.reference.expanduser().resolve()
    broken = args.broken.expanduser().resolve()
    output = args.output.expanduser().resolve()

    if not reference.exists():
        print(f"Reference file does not exist: {reference}", file=sys.stderr)
        return 2
    if not broken.exists():
        print(f"Broken file does not exist: {broken}", file=sys.stderr)
        return 2
    if not _validate_input_file(reference, "Reference"):
        return 2
    if not _validate_input_file(broken, "Broken"):
        return 2

    if args.keep_workdir:
        workdir = args.keep_workdir.expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        workdir = Path(tempfile.mkdtemp(prefix="dji-recover-"))
        cleanup = True

    try:
        print(f"Workdir: {workdir}", file=sys.stderr)
        _print_reference_summary(reference)

        print("Extracting VPS/SPS/PPS from reference...", file=sys.stderr)
        parameter_sets = extract_parameter_sets(reference, workdir)
        print(
            "Parameter sets: "
            f"VPS={len(parameter_sets.vps)} bytes, "
            f"SPS={len(parameter_sets.sps)} bytes, "
            f"PPS={len(parameter_sets.pps)} bytes",
            file=sys.stderr,
        )

        hevc_path = workdir / "recovered.hevc"
        frame_filter = args.frame_filter
        if frame_filter == "auto":
            frame_filter = "pairs" if args.timeline == "clean" else "none"
        insert_aud = args.hevc_aud == "on" or (args.hevc_aud == "auto" and args.timeline == "clean")
        print("Recovering length-prefixed HEVC NAL units...", file=sys.stderr)
        stats = recover_hevc_annexb(
            broken=broken,
            output_hevc=hevc_path,
            parameter_sets=parameter_sets,
            start_offset=parse_offset(args.start_offset),
            max_scan=parse_offset(args.max_scan),
            max_nal_size=parse_offset(args.max_nal_size) or 512 * 1024,
            frame_filter=frame_filter,
            insert_aud=insert_aud,
            gop_start=args.gop_start,
        )
        print(
            f"Recovered {stats.nals_written} NAL units from 0x{stats.start_offset:x}; "
            f"resyncs={stats.resyncs}, invalid_words={stats.invalid_words}, "
            f"last_input_offset=0x{stats.last_output_offset:x}",
            file=sys.stderr,
        )
        if frame_filter != "none":
            print(
                f"Frame filter: {frame_filter}; "
                f"frames_written={stats.frames_written}, frames_dropped={stats.frames_dropped}",
                file=sys.stderr,
            )
        if insert_aud:
            print("Inserted HEVC access unit delimiters.", file=sys.stderr)
        if args.gop_start == "next-idr":
            print("Started video at the next detected IDR GOP.", file=sys.stderr)

        audio = _resolve_audio(
            audio_source=args.audio_source,
            reference=reference,
            broken=broken,
            workdir=workdir,
            video_ranges=stats.video_ranges,
            enabled=args.audio == "auto",
            audio_mode=args.audio_mode,
            audio_recovery=args.audio_recovery,
        )
        mode = args.mode or ("reencode" if args.timeline == "clean" else "copy")
        print(f"Writing MP4 ({args.timeline}/{mode})...", file=sys.stderr)
        if mode == "copy":
            print(
                "Note: preserve/copy keeps damaged HEVC as-is; if players show black video, retry with --timeline clean.",
                file=sys.stderr,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        mux_hevc_to_mp4(
            hevc_path,
            output,
            frame_rate=args.frame_rate,
            mode=mode,
            audio=audio,
            audio_sync=args.audio_sync,
        )
        print(f"Recovered MP4: {output}", file=sys.stderr)
        if args.gop_report:
            report = args.gop_report.expanduser().resolve()
            samples_dir = args.gop_samples_dir.expanduser().resolve() if args.gop_samples_dir else None
            print("Inspecting recovered GOP/keyframe quality...", file=sys.stderr)
            samples = inspect_gops(
                output,
                report,
                samples_dir=samples_dir,
                limit=max(0, args.gop_report_limit),
            )
            worst = sorted(samples, key=lambda sample: sample.green_ratio, reverse=True)[:5]
            print(f"GOP quality report: {report}", file=sys.stderr)
            if worst:
                summary = ", ".join(f"{sample.time_seconds:.2f}s={sample.green_ratio:.1%}" for sample in worst)
                print(f"Worst green ratios: {summary}", file=sys.stderr)
        return 0
    except RecoveryError as exc:
        print(f"Recovery failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)


def _validate_input_file(path: Path, label: str) -> bool:
    if not path.is_file():
        print(f"{label} path is not a file: {path}", file=sys.stderr)
        return False
    try:
        size = path.stat().st_size
    except OSError as exc:
        print(f"{label} file cannot be inspected: {path}: {exc}", file=sys.stderr)
        return False
    if size == 0:
        print(f"{label} file is empty (0 bytes): {path}", file=sys.stderr)
        return False
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        print(f"{label} file cannot be read: {path}: {exc}", file=sys.stderr)
        return False
    return True


def _print_reference_summary(reference: Path) -> None:
    try:
        info = ffprobe_json(reference)
    except Exception as exc:
        print(f"Warning: could not probe reference: {exc}", file=sys.stderr)
        return
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return
    codec = video.get("codec_name", "?")
    profile = video.get("profile", "?")
    width = video.get("width", "?")
    height = video.get("height", "?")
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "?"
    print(f"Reference video: {codec} {profile}, {width}x{height}, {rate} fps", file=sys.stderr)


def _resolve_audio(
    audio_source: Path | None,
    reference: Path,
    broken: Path,
    workdir: Path,
    video_ranges,
    enabled: bool,
    audio_mode: str,
    audio_recovery: str,
) -> Path | None:
    if not enabled:
        print("Audio disabled.", file=sys.stderr)
        return None

    if audio_source:
        audio = audio_source.expanduser().resolve()
        if not audio.exists():
            raise FileNotFoundError(f"Audio source does not exist: {audio}")
        return _prepare_audio(audio, workdir, audio_mode)

    extracted = workdir / "audio.m4a"
    print("Trying best-effort audio extraction from broken file...", file=sys.stderr)
    if try_extract_audio(broken, extracted):
        print("Recovered an audio stream with ffmpeg.", file=sys.stderr)
        return _prepare_audio(extracted, workdir, audio_mode)

    print("Trying DJI raw AAC recovery from video gaps...", file=sys.stderr)
    audio_info = _reference_audio_info(reference)
    adts = workdir / "recovered-audio.aac"
    audio_stats = recover_dji_aac_adts(
        broken=broken,
        output_adts=adts,
        video_ranges=video_ranges,
        sample_rate=audio_info["sample_rate"],
        channels=audio_info["channels"],
        guess_final_frames=audio_recovery == "guess",
    )
    if audio_stats.frames_written > 0:
        print(
            f"Recovered {audio_stats.frames_written} AAC frames "
            f"({audio_stats.duration_seconds:.1f}s); "
            f"exact_frames={audio_stats.exact_frames}, "
            f"guessed_last_frames={audio_stats.guessed_last_frames}",
            file=sys.stderr,
        )
        if audio_stats.guessed_ratio >= 0.25:
            print(
                "Warning: many recovered AAC frames were inferred from DJI gap boundaries; "
                "--audio-recovery exact can diagnose this but may make audio much shorter.",
                file=sys.stderr,
            )
        return _prepare_audio(adts, workdir, audio_mode)

    print("No recoverable audio found; continuing with video only.", file=sys.stderr)
    return None


def _prepare_audio(audio: Path, workdir: Path, audio_mode: str) -> Path:
    if audio_mode == "copy":
        return audio
    cleaned = workdir / "recovered-audio-clean.m4a"
    print("Transcoding recovered audio for MP4 compatibility...", file=sys.stderr)
    if transcode_audio_to_m4a(audio, cleaned):
        return cleaned
    print("Audio transcode failed; muxing original recovered audio.", file=sys.stderr)
    return audio


def _reference_audio_info(reference: Path) -> dict[str, int]:
    try:
        info = ffprobe_json(reference)
    except Exception:
        return {"sample_rate": 48000, "channels": 2}
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        return {"sample_rate": 48000, "channels": 2}
    return {
        "sample_rate": int(audio.get("sample_rate") or 48000),
        "channels": int(audio.get("channels") or 2),
    }
