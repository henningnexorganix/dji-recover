from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def mux_hevc_to_mp4(
    hevc: Path,
    output: Path,
    frame_rate: str,
    mode: str,
    audio: Path | None = None,
) -> None:
    if audio is not None:
        with tempfile.TemporaryDirectory(prefix="dji-recover-mux-", dir=output.parent) as tmp:
            video_only = Path(tmp) / "video-only.mp4"
            mux_hevc_to_mp4(hevc, video_only, frame_rate=frame_rate, mode=mode, audio=None)
            run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-i",
                    str(video_only),
                    "-i",
                    str(audio),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
        return

    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts"]
    if frame_rate:
        base += ["-r", frame_rate]
    if mode == "copy":
        base += ["-i", str(hevc)]
        video_args = ["-c:v", "copy", "-tag:v", "hvc1"]
    elif mode == "reencode":
        base += ["-err_detect", "ignore_err", "-i", str(hevc)]
        video_args = [
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
    else:
        raise ValueError(f"Unknown mux mode: {mode}")

    cmd = base + ["-map", "0:v:0"]
    cmd += video_args + ["-movflags", "+faststart", str(output)]
    run(cmd)


def transcode_audio_to_m4a(source: Path, output: Path, bitrate: str = "320k") -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-err_detect",
        "ignore_err",
        "-i",
        str(source),
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        str(output),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def try_extract_audio(source: Path, output: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-c:a",
        "copy",
        str(output),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0
