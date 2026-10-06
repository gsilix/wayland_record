# Wayland Meeting Recorder

Record a selected monitor, system audio, and your microphone. Press **Ctrl+C** to save an H.264/AAC MP4. Audio routing stays unchanged.

## Installation

Requires Bash 4+, a Wayland compositor supported by `wf-recorder` (such as Sway), and a working PipeWire setup with PulseAudio compatibility and WirePlumber. The commands below install the recording tools; they assume your audio setup is already configured.

### Ubuntu / Debian / Linux Mint

```bash
sudo apt update
sudo apt install wf-recorder slurp ffmpeg pipewire-bin pulseaudio-utils
```

On Ubuntu, enable the **Universe** repository if `wf-recorder` is unavailable ([package details](https://packages.ubuntu.com/noble/wf-recorder)).

### Arch Linux / Manjaro

```bash
sudo pacman -Syu wf-recorder slurp ffmpeg pipewire-audio libpulse
```

`pw-record` is included in [pipewire-audio](https://archlinux.org/packages/extra/x86_64/pipewire-audio/files/).

### Fedora (DNF)

Enable [RPM Fusion Free](https://docs.fedoraproject.org/en-US/quick-docs/rpmfusion-setup/) for FFmpeg with H.264 encoding:

```bash
sudo dnf install "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
sudo dnf install wf-recorder slurp ffmpeg pipewire-utils pulseaudio-utils --allowerasing
```

The second command replaces conflicting Fedora FFmpeg packages with the RPM Fusion version.

### openSUSE Tumbleweed

```bash
sudo zypper install wf-recorder slurp ffmpeg pipewire-tools pulseaudio-utils
```

For `libx264` encoding, use the [Packman codec packages](https://en.opensuse.org/SDB:Installing_codecs_from_Packman_repositories).

## Usage

```bash
chmod +x record_meeting.sh
./record_meeting.sh
```

Click the monitor to record; **Esc** cancels selection. Press **Ctrl+C** in the terminal to stop, then wait for processing.

The final file is saved in the current directory as `conference_YYYY-MM-DD_HH-MM-SS.mp4`. Existing files are preserved by adding a numeric suffix. After success, the entire working directory is deleted. On failure, sources and logs are kept in that directory for recovery.

## Options

| Option | Default | Purpose |
| --- | --- | --- |
| `-h`, `--help` | — | Show help |
| `-o`, `--output-dir DIR` | Current directory | Choose where to save recordings |
| `--crf NUMBER` | `23` | Video quality, 0–51; lower means better quality and larger files |
| `--preset NAME` | `ultrafast` | x264 speed/compression preset |
| `--system-offset MS` | `0` | Shift system audio relative to video |
| `--mic-offset MS` | `0` | Shift microphone audio relative to video |

```bash
./record_meeting.sh --output-dir "$HOME/Videos" --crf 20 --preset veryfast
```

## Notes

- Select the default output and microphone before starting. System audio includes all applications playing through that output.
- X11 and Wayland compositors without the required capture protocols are unsupported.
- Make a short test recording to check both audio sources and timing. Positive offsets delay audio; negative offsets advance it. Fixed offsets do not correct timing drift.
- If recording fails, inspect `system.log`, `mic.log`, `video.log`, and `merge.log` in the retained working directory.

## Development

```bash
bash -n record_meeting.sh
shellcheck record_meeting.sh
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE).
