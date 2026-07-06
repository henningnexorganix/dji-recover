# dji-recover

`dji-recover` is an experimental command-line tool for recovering crashed DJI MP4
files where the media data is still present but the `moov` atom is missing.

The initial target is DJI Osmo Action 4 footage using HEVC/H.265 video and AAC
audio. The tool was built around files reported by `ffmpeg` as:

```text
moov atom not found
```

It uses a known-good reference clip from the same camera and settings to recover
the missing HEVC parameter sets, then scans the damaged file for DJI-style video
and audio data.

## Status

This is alpha recovery software.

It can already recover playable video from some DJI Osmo Action 4 crash files,
including files that QuickTime and DaVinci Resolve can open after repair. It can
also attempt best-effort AAC recovery. Damaged sections of the source file may
still appear as freezes, glitches, dropped segments, or missing audio.

Always keep the original damaged file untouched.

## Supported Footage

The first tested profile is:

- DJI Osmo Action 4
- HEVC Main 10, `hvc1`
- 2688x1512
- 23.976 fps
- AAC 48 kHz stereo, when recoverable

Other DJI cameras, resolutions, frame rates, and audio layouts may work, but they
are not yet well tested.

## Requirements

- Python 3.10 or newer
- `ffmpeg`
- `ffprobe`

On macOS with Homebrew:

```sh
brew install ffmpeg
```

## Install

For normal use, install `dji-recover` with `pipx`. This gives you a regular
`dji-recover` command without activating a virtual environment manually.

```sh
brew install pipx
pipx ensurepath
pipx install git+https://github.com/henningnexorganix/dji-recover.git
```

Restart your shell if `pipx ensurepath` asks you to. After installation, the CLI
is available as:

```sh
dji-recover --help
```

The command is `dji-recover`, not `dji-recovery`. If your shell says
`command not found`, check that `pipx` put its command directory on your `PATH`:

```sh
pipx ensurepath
which dji-recover
dji-recover --help
```

To upgrade or remove the installed command later:

```sh
pipx upgrade dji-recover
pipx uninstall dji-recover
```

### Developer Install

For local development, use an editable install in a project virtual environment:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

You can also run the source tree directly:

```sh
PYTHONPATH=src python3 -m dji_recover --help
```

## Quick Start

Use a playable reference MP4 recorded by the same camera with the same settings:

```sh
dji-recover \
  --reference good-from-same-camera.MP4 \
  --broken crashed-without-moov.MP4 \
  --output recovered.mp4
```

If you already know where the DJI HEVC payload starts, provide the offset:

```sh
dji-recover \
  --reference good-from-same-camera.MP4 \
  --broken crashed-without-moov.MP4 \
  --output recovered.mp4 \
  --start-offset 0x267a \
  --frame-rate 24000/1001
```

## Recovery Modes

`dji-recover` has two timeline modes.

`preserve` is the default. It keeps every recoverable video unit and preserves
timing as much as possible. This is the best first-pass recovery mode and is
usually the right choice for an archival master. If the source data has damaged
sections, playback may freeze until the decoder can continue.

```sh
dji-recover \
  --reference good.MP4 \
  --broken broken.MP4 \
  --output recovered-preserve.mp4 \
  --timeline preserve
```

`clean` decodes the recovered HEVC stream, writes H.264 video, and transcodes
recovered audio. It is slower and loses one generation of quality, but often
creates a much friendlier file for QuickTime, DaVinci Resolve, and other players
when the raw recovered HEVC stream is damaged. By default, `clean` also applies
a stricter DJI frame-pair filter before transcoding, which drops incomplete video
frames that can otherwise make decoders blend unrelated damaged scenes.

```sh
dji-recover \
  --reference good.MP4 \
  --broken broken.MP4 \
  --output recovered-clean.mp4 \
  --timeline clean
```

Lower-level video behavior can be overridden with `--mode copy|reencode`.
`preserve` defaults to HEVC `copy`; `clean` defaults to H.264 `reencode`.
The access-unit filter can be overridden with `--frame-filter auto|none|complete|pairs`.

## Audio Recovery

Audio recovery is best effort.

If `--audio-source some.aac` or `--audio-source some.m4a` is supplied, that file
is muxed into the recovered MP4. Otherwise, the tool first asks ffmpeg to extract
an audio stream from the broken MP4. That usually fails when the `moov` atom is
missing, so the tool then scans the gaps between recovered DJI HEVC video units
for raw AAC frames.

Recovered audio is transcoded by default for MP4 compatibility:

```sh
dji-recover \
  --reference good.MP4 \
  --broken broken.MP4 \
  --output recovered.mp4 \
  --audio-mode transcode
```

To mux recovered AAC directly:

```sh
dji-recover \
  --reference good.MP4 \
  --broken broken.MP4 \
  --output recovered.mp4 \
  --audio-mode copy
```

To disable audio entirely:

```sh
dji-recover \
  --reference good.MP4 \
  --broken broken.MP4 \
  --output recovered-video-only.mp4 \
  --audio none
```

## Useful Options

```text
--reference PATH       Known-good MP4 from the same camera/settings
--broken PATH          Damaged MP4 to recover
--output PATH          Recovered MP4 output path
--start-offset OFFSET  Known HEVC payload offset, decimal or hex
--frame-rate RATE      Frame rate for raw HEVC timestamps
--timeline MODE        preserve or clean
--mode MODE            copy or reencode
--frame-filter MODE    auto, none, complete, or pairs
--audio MODE           auto or none
--audio-mode MODE      transcode or copy
--audio-source PATH    External audio file to mux
--keep-workdir PATH    Keep intermediate files for inspection
--max-scan BYTES       Auto-detect scan limit, decimal or hex
--max-nal-size BYTES   Maximum plausible HEVC NAL size
```

## How It Works

1. Converts the reference clip's first video seconds to Annex B with ffmpeg.
2. Parses VPS/SPS/PPS from the reference stream.
3. Index-scans the damaged file for DJI-style length-prefixed HEVC slice NAL
   units.
4. Converts valid HEVC NAL units to Annex B and rejects likely false positives.
5. Uses the recovered video intervals to scan interleaved gaps for DJI raw AAC
   frames.
6. Wraps recovered AAC in ADTS and optionally transcodes it.
7. Builds an MP4 with `hvc1` video tagging and `+faststart`.

When audio is present, the tool first creates a timed video-only MP4, then muxes
that MP4 with the recovered audio. This avoids losing audio when muxing raw HEVC
input that lacks stable timestamps.

## Development

Run the test suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Large camera files and recovery outputs should not be committed. The repository
ignores `Downloads/`, common video/audio outputs, and intermediate recovery
streams.

## Legal Notes

This project is a clean Python implementation. It does not vendor or copy
`djifix`, `untrunc`, camera firmware, or embedded DJI parameter-set tables.
Instead, it derives codec parameter sets from a user-supplied reference clip.

DJI is a trademark of its respective owner. This project is not affiliated with
or endorsed by DJI.

## License

`dji-recover` is licensed under the GNU General Public License v3.0 or later.
See [LICENSE](LICENSE).
