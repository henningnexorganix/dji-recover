from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
import subprocess


@dataclass
class GopSample:
    index: int
    time_seconds: float
    pict_type: str
    green_ratio: float
    dark_ratio: float
    sample_path: str | None = None


@dataclass(frozen=True)
class DropInterval:
    start: float
    end: float
    green_ratio: float


def inspect_gops(
    video: Path,
    report: Path,
    samples_dir: Path | None = None,
    limit: int = 200,
    width: int = 320,
    height: int = 180,
) -> list[GopSample]:
    frames = _keyframes(video)
    if limit > 0:
        frames = frames[:limit]

    if samples_dir is not None:
        samples_dir.mkdir(parents=True, exist_ok=True)

    samples: list[GopSample] = []
    for index, frame in enumerate(frames):
        time_seconds = float(frame["best_effort_timestamp_time"])
        rgb = _extract_rgb_frame(video, time_seconds, width, height)
        sample_path = None
        if samples_dir is not None:
            sample_path = str(samples_dir / f"gop-{index:04d}-{time_seconds:010.3f}.jpg")
            _extract_jpeg_frame(video, time_seconds, Path(sample_path))
        samples.append(
            GopSample(
                index=index,
                time_seconds=time_seconds,
                pict_type=frame.get("pict_type", "?"),
                green_ratio=_green_ratio(rgb),
                dark_ratio=_dark_ratio(rgb),
                sample_path=sample_path,
            )
        )

    _write_report(video, report, width, height, samples, sample_mode="keyframe")
    return samples


def inspect_time_samples(
    video: Path,
    report: Path,
    samples_dir: Path | None = None,
    interval: float = 1.0,
    width: int = 320,
    height: int = 180,
) -> list[GopSample]:
    duration = video_duration(video)
    if samples_dir is not None:
        samples_dir.mkdir(parents=True, exist_ok=True)

    samples: list[GopSample] = []
    time_seconds = 0.0
    index = 0
    while time_seconds < duration:
        rgb = _extract_rgb_frame(video, time_seconds, width, height)
        sample_path = None
        if samples_dir is not None:
            sample_path = str(samples_dir / f"time-{index:04d}-{time_seconds:010.3f}.jpg")
            _extract_jpeg_frame(video, time_seconds, Path(sample_path))
        samples.append(
            GopSample(
                index=index,
                time_seconds=time_seconds,
                pict_type="time",
                green_ratio=_green_ratio(rgb),
                dark_ratio=_dark_ratio(rgb),
                sample_path=sample_path,
            )
        )
        index += 1
        time_seconds += max(0.1, interval)

    _write_report(video, report, width, height, samples, sample_mode="time")
    return samples


def bad_gop_intervals(
    samples: list[GopSample],
    video_duration: float,
    green_threshold: float,
) -> list[DropInterval]:
    intervals: list[DropInterval] = []
    ordered = sorted(samples, key=lambda sample: sample.time_seconds)
    for index, sample in enumerate(ordered):
        if sample.green_ratio < green_threshold:
            continue
        end = ordered[index + 1].time_seconds if index + 1 < len(ordered) else video_duration
        if end > sample.time_seconds:
            intervals.append(DropInterval(sample.time_seconds, end, sample.green_ratio))
    return intervals


def drop_intervals(
    source: Path,
    output: Path,
    intervals: list[DropInterval],
    frame_rate: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not intervals:
        if source.resolve() != output.resolve():
            shutil.copyfile(source, output)
        return

    video_select = _keep_expression(intervals)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        f"select='{video_select}',setpts=N/({frame_rate})/TB",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "avc1",
    ]

    if _has_audio(source):
        audio_select = _keep_expression(intervals)
        cmd += [
            "-map",
            "0:a:0",
            "-af",
            f"aselect='{audio_select}',asetpts=N/SR/TB",
            "-c:a",
            "aac",
            "-b:a",
            "320k",
        ]

    cmd += ["-movflags", "+faststart", str(output)]
    subprocess.run(cmd, check=True)


def video_duration(video: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(video),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return float(result.stdout.strip())


def _keyframes(video: Path) -> list[dict]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,pict_type",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    data = json.loads(result.stdout)
    return [
        frame
        for frame in data.get("frames", [])
        if frame.get("best_effort_timestamp_time") is not None
    ]


def _write_report(
    video: Path,
    report: Path,
    width: int,
    height: int,
    samples: list[GopSample],
    sample_mode: str,
) -> None:
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "video": str(video),
                "sample_mode": sample_mode,
                "sample_width": width,
                "sample_height": height,
                "samples": [asdict(sample) for sample in samples],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _has_audio(video: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return bool(result.stdout.strip())


def _keep_expression(intervals: list[DropInterval]) -> str:
    drops = "+".join(f"between(t,{interval.start:.6f},{interval.end:.6f})" for interval in intervals)
    return f"not({drops})"


def _extract_rgb_frame(video: Path, time_seconds: float, width: int, height: int) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{time_seconds:.6f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:-1:-1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def _extract_jpeg_frame(video: Path, time_seconds: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{time_seconds:.6f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )


def _green_ratio(rgb: bytes) -> float:
    if not rgb:
        return 0.0
    green = 0
    pixels = len(rgb) // 3
    for i in range(0, pixels * 3, 3):
        red = rgb[i]
        green_channel = rgb[i + 1]
        blue = rgb[i + 2]
        if green_channel > 90 and green_channel > red * 1.45 and green_channel > blue * 1.45:
            green += 1
    return green / pixels


def _dark_ratio(rgb: bytes) -> float:
    if not rgb:
        return 0.0
    dark = 0
    pixels = len(rgb) // 3
    for i in range(0, pixels * 3, 3):
        if rgb[i] + rgb[i + 1] + rgb[i + 2] < 45:
            dark += 1
    return dark / pixels
