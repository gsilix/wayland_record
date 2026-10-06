#!/usr/bin/env bash
# Record a Wayland monitor, system audio, and the default microphone.
set -uo pipefail
umask 077

usage() {
    printf '%s\n' 'Usage: record_meeting.sh [OPTIONS]

Click a monitor, record the default output and microphone, then press Ctrl+C.

Options:
  -h, --help                 Show this help and exit
  -o, --output-dir DIR       Save recordings under DIR (default: current directory)
      --crf NUMBER           H.264 quality, 0–51; lower is better (default: 23)
      --preset NAME          x264 speed preset (default: ultrafast)
      --system-offset MS     Shift system audio relative to video (default: 0)
      --mic-offset MS        Shift microphone audio relative to video (default: 0)

Positive offsets delay audio; negative offsets trim audio from the beginning.
After a successful merge, only the dated MP4 remains. Failed sources and logs are retained.'
}

error() { printf 'Error: %s\n' "$*" >&2; }
fail() { error "$*"; exit 1; }

OUTPUT_DIR=.
CRF=23
PRESET=ultrafast
SYSTEM_OFFSET=0
MIC_OFFSET=0
PIDS=()
LABELS=()
SESSION_DIR=
STOP_REQUESTED=0
STOP_FAILED=0

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -o|--output-dir|--crf|--preset|--system-offset|--mic-offset)
            (($# >= 2)) || fail "Missing value for $1."
            case "$1" in
                -o|--output-dir) OUTPUT_DIR=$2 ;;
                --crf) CRF=$2 ;;
                --preset) PRESET=$2 ;;
                --system-offset) SYSTEM_OFFSET=$2 ;;
                --mic-offset) MIC_OFFSET=$2 ;;
            esac
            shift 2 ;;
        *) error "Unknown option: $1"; usage >&2; exit 1 ;;
    esac
done

[[ $CRF =~ ^[0-9]{1,2}$ ]] || fail 'CRF must be an integer from 0 to 51.'
CRF=$((10#$CRF))
((CRF <= 51)) || fail 'CRF must be an integer from 0 to 51.'
case "$PRESET" in
    ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow) ;;
    *) fail 'Invalid x264 preset. See --help and README.md.' ;;
esac
for offset in "$SYSTEM_OFFSET" "$MIC_OFFSET"; do
    [[ $offset =~ ^-?[0-9]{1,6}$ ]] || fail 'Audio offsets must be integer milliseconds between -600000 and 600000.'
    if [[ $offset == -* ]]; then
        value=$((-10#${offset#-}))
    else
        value=$((10#$offset))
    fi
    ((value >= -600000 && value <= 600000)) || fail 'Audio offset exceeds ten minutes.'
done
# Normalize leading zeros before arithmetic and FFmpeg filter construction.
if [[ $SYSTEM_OFFSET == -* ]]; then SYSTEM_OFFSET=$((-10#${SYSTEM_OFFSET#-})); else SYSTEM_OFFSET=$((10#$SYSTEM_OFFSET)); fi
if [[ $MIC_OFFSET == -* ]]; then MIC_OFFSET=$((-10#${MIC_OFFSET#-})); else MIC_OFFSET=$((10#$MIC_OFFSET)); fi
[[ -n $OUTPUT_DIR ]] || fail 'Output directory cannot be empty.'

missing=()
for dependency in slurp pactl pw-record wf-recorder ffmpeg ffprobe awk date mktemp mkdir ln rm sleep; do
    command -v "$dependency" >/dev/null 2>&1 || missing+=("$dependency")
done
((${#missing[@]} == 0)) || fail "Missing dependencies: ${missing[*]}. Install them with your distribution's package manager."
[[ -n ${WAYLAND_DISPLAY:-} ]] || fail 'No Wayland session detected (WAYLAND_DISPLAY is unset).'

# Match whole device names, rather than treating names as regular expressions.
DEFAULT_SINK=$(pactl get-default-sink) || fail 'Could not query the default audio output.'
DEFAULT_SOURCE=$(pactl get-default-source) || fail 'Could not query the default audio source.'
[[ -n $DEFAULT_SINK && -n $DEFAULT_SOURCE ]] || fail 'Default audio devices are not configured.'
[[ $DEFAULT_SOURCE != *.monitor ]] || fail 'Default input is an output monitor. Select a microphone or native virtual audio source.'
SINKS=$(pactl list short sinks) || fail 'Could not list audio outputs.'
SOURCES=$(pactl list short sources) || fail 'Could not list audio sources.'
awk -v name="$DEFAULT_SINK" '$2 == name { found=1 } END { exit !found }' <<< "$SINKS" || fail 'Default audio output was not found.'
awk -v name="$DEFAULT_SOURCE" '$2 == name { found=1 } END { exit !found }' <<< "$SOURCES" || fail 'Default audio source was not found.'

printf 'Click the monitor you want to record (Esc cancels)...\n'
MONITOR_GEOMETRY=$(slurp -o) || { printf 'Monitor selection canceled.\n'; exit 0; }
[[ $MONITOR_GEOMETRY =~ ^-?[0-9]+,-?[0-9]+\ [0-9]+x[0-9]+$ ]] || fail 'Invalid monitor geometry returned by slurp.'

mkdir -p -- "$OUTPUT_DIR" || fail "Could not create output directory: $OUTPUT_DIR"
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd -P) || fail 'Could not resolve the output directory.'
SESSION_DIR=$(mktemp -d "$OUTPUT_DIR/conference_$(date +%F_%H-%M-%S)_XXXXXX") || fail 'Could not create a recording directory.'
OUTPUT_FILE="$SESSION_DIR/recording.mp4"

captures_running() {
    local pid
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then return 0; fi
    done
    return 1
}

stop_captures() {
    ((${#PIDS[@]})) || return 0
    local pid deadline
    for pid in "${PIDS[@]}"; do kill -INT "$pid" 2>/dev/null || :; done
    deadline=$((SECONDS + 5))
    while captures_running && ((SECONDS < deadline)); do sleep 0.1; done
    if captures_running; then
        for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null || :; done
        deadline=$((SECONDS + 2))
        while captures_running && ((SECONDS < deadline)); do sleep 0.1; done
    fi
    if captures_running; then
        error 'A capture process did not stop gracefully. Source files will be kept.'
        STOP_FAILED=1
        for pid in "${PIDS[@]}"; do kill -KILL "$pid" 2>/dev/null || :; done
    fi
    for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || :; done
    PIDS=()
}

cleanup() {
    local status=$?
    trap - EXIT
    trap '' INT TERM HUP
    stop_captures
    if ((status != 0)); then
        printf 'Source recordings and logs retained in: %s\n' "$SESSION_DIR" >&2
    fi
    exit "$status"
}
trap cleanup EXIT
# Signal handlers only request a stop; finalization runs once in the main flow.
trap 'STOP_REQUESTED=1' INT TERM HUP

# Keep inputs, logs, and the chosen settings together for recovery.
{
    printf 'Output device: %s\nInput device: %s\nGeometry: %s\n' "$DEFAULT_SINK" "$DEFAULT_SOURCE" "$MONITOR_GEOMETRY"
    printf 'CRF: %s\nPreset: %s\nSystem offset (ms): %s\nMicrophone offset (ms): %s\n' "$CRF" "$PRESET" "$SYSTEM_OFFSET" "$MIC_OFFSET"
} > "$SESSION_DIR/session.txt" || fail 'Could not write session metadata.'

# PipeWire accepts node names. Explicitly capture the output's monitor ports.
pw-record --target "$DEFAULT_SINK" --rate 48000 \
    --properties '{"stream.capture.sink":true,"node.dont-fallback":true,"node.dont-reconnect":true}' \
    "$SESSION_DIR/system.wav" > "$SESSION_DIR/system.log" 2>&1 &
PIDS+=("$!"); LABELS+=("System audio")
pw-record --target "$DEFAULT_SOURCE" --rate 48000 \
    --properties '{"node.dont-fallback":true,"node.dont-reconnect":true}' \
    "$SESSION_DIR/mic.wav" > "$SESSION_DIR/mic.log" 2>&1 &
PIDS+=("$!"); LABELS+=("Microphone")
wf-recorder -g "$MONITOR_GEOMETRY" -c libx264 -p "preset=$PRESET" -p "crf=$CRF" \
    -f "$SESSION_DIR/video.mp4" > "$SESSION_DIR/video.log" 2>&1 &
PIDS+=("$!"); LABELS+=("Video")

check_captures() {
    local index status
    for index in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$index]}" 2>/dev/null; then
            status=0
            wait "${PIDS[$index]}" || status=$?
            # Remove the reaped PID so cleanup cannot signal a reused PID.
            unset 'PIDS[index]'
            fail "${LABELS[$index]} capture stopped unexpectedly (exit $status). Check the session logs."
        fi
    done
}

sleep 0.5
if ((STOP_REQUESTED == 0)); then
    check_captures
    printf 'Recording started. Press Ctrl+C to stop and save.\n'
    printf 'Output: %s\nMicrophone: %s\nSession: %s\n' "$DEFAULT_SINK" "$DEFAULT_SOURCE" "$SESSION_DIR"
fi
while ((STOP_REQUESTED == 0)); do
    check_captures
    sleep 0.2
done

printf '\nStopping capture...\n'
trap '' INT TERM HUP
stop_captures
((STOP_FAILED == 0)) || fail 'Recording could not be finalized safely.'
for input in video.mp4 system.wav mic.wav; do
    [[ -s $SESSION_DIR/$input ]] || fail "Missing or empty source file: $input"
done
VIDEO_DURATION=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration \
    -of default=noprint_wrappers=1:nokey=1 "$SESSION_DIR/video.mp4" 2>> "$SESSION_DIR/merge.log") || fail 'Could not inspect the video recording.'
[[ $VIDEO_DURATION =~ ^[0-9]+([.][0-9]+)?$ ]] || fail 'Video has no usable duration.'
awk -v duration="$VIDEO_DURATION" 'BEGIN { exit !(duration > 0) }' || fail 'Video duration is zero.'

# Calibrated offsets operate on samples, then pad/trim the mix to video length.
audio_filter() {
    local input=$1 offset=$2 label=$3 trim_ms
    if ((offset < 0)); then
        trim_ms=$((-offset))
        printf '[%s:a]atrim=start=%d.%03d,asetpts=PTS-STARTPTS[%s]' "$input" "$((trim_ms / 1000))" "$((trim_ms % 1000))" "$label"
    else
        printf '[%s:a]asetpts=PTS-STARTPTS,adelay=%s:all=1[%s]' "$input" "$offset" "$label"
    fi
}
FILTER="$(audio_filter 1 "$SYSTEM_OFFSET" system);$(audio_filter 2 "$MIC_OFFSET" mic);[system][mic]amix=inputs=2:duration=longest:normalize=1,apad,atrim=duration=${VIDEO_DURATION}[a]"
printf 'Combining video and audio...\n'
if ! ffmpeg -nostdin -n -i "$SESSION_DIR/video.mp4" -i "$SESSION_DIR/system.wav" -i "$SESSION_DIR/mic.wav" \
    -filter_complex "$FILTER" -map 0:v:0 -map '[a]' -c:v copy -c:a aac -b:a 192k \
    -movflags +faststart "$OUTPUT_FILE" > "$SESSION_DIR/merge.log" 2>&1; then
    fail 'FFmpeg processing failed. Check merge.log; sources are retained.'
fi
[[ -s $OUTPUT_FILE ]] || fail 'FFmpeg did not create a usable output file.'
# Publish the complete file atomically without overwriting an existing recording.
# The session directory is on the same filesystem as the final output.
OUTPUT_BASE="$OUTPUT_DIR/conference_$(date +%F_%H-%M-%S)"
OUTPUT_FILE="$OUTPUT_BASE.mp4"
suffix=0
while ! ln -- "$SESSION_DIR/recording.mp4" "$OUTPUT_FILE" 2> "$SESSION_DIR/publish.log"; do
    [[ -e $OUTPUT_FILE || -L $OUTPUT_FILE ]] || fail 'Could not save the final MP4 in the output directory. Check publish.log.'
    suffix=$((suffix + 1))
    OUTPUT_FILE="${OUTPUT_BASE}_$suffix.mp4"
done
rm -rf -- "$SESSION_DIR" || fail 'Could not remove the working directory; the final recording is saved.'
printf 'Recording complete! Saved to: %s\n' "$OUTPUT_FILE"
