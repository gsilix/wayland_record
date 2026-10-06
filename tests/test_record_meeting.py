"""Integration checks using fake devices; no screen or microphone is accessed."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "record_meeting.sh"
REAL_FFMPEG = os.environ.get("RECORDER_TEST_FFMPEG") or shutil.which("ffmpeg")
REAL_FFPROBE = shutil.which("ffprobe")

FAKE_TOOL = r'''
import json, os, pathlib, shutil, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_EVENTS"], "a") as f:
    f.write(json.dumps({"tool": name, "pid": os.getpid(), "args": args}) + "\n")
if name == "date":
    print("2026-10-06_14-30-00")
elif name == "slurp":
    if os.environ.get("TEST_CANCEL"): sys.exit(1)
    print("0,0 1280x720")
elif name == "pactl":
    if os.environ.get("TEST_PACTL_FAIL"): sys.exit(1)
    sink = "alsa_output.test+sink.1"
    source = "alsa_input.test+mic.1"
    if os.environ.get("TEST_MONITOR_SOURCE"): source = sink + ".monitor"
    if args == ["get-default-sink"]: print(sink)
    elif args == ["get-default-source"]: print(source)
    elif args == ["list", "short", "sinks"]:
        print("10\t" + sink + ".other\tdriver\tformat")
        if not os.environ.get("TEST_NO_EXACT_SINK"): print("11\t" + sink + "\tdriver\tformat")
    elif args == ["list", "short", "sources"]: print("12\t" + source + "\tdriver\tformat")
    else: sys.exit(1)
elif name == "ffprobe":
    if os.environ.get("TEST_PROBE_FAIL"): sys.exit(1)
    print(os.environ.get("TEST_DURATION", "2.000"))
elif name == "ffmpeg":
    if os.environ.get("TEST_MERGE_FAIL"): sys.exit(1)
    pathlib.Path(args[-1]).write_bytes(b"final video")
else:
    output = pathlib.Path(args[args.index("-f") + 1] if name == "wf-recorder" else args[-1])
    kind = "video" if name == "wf-recorder" else ("system" if output.name == "system.wav" else "mic")
    if os.environ.get("TEST_CAPTURE_FAIL") == kind: sys.exit(7)
    if os.environ.get("TEST_MEDIA"):
        shutil.copyfile(pathlib.Path(os.environ["TEST_MEDIA"]) / output.name, output)
    elif os.environ.get("TEST_EMPTY") != kind: output.write_bytes(b"source recording")
    if os.environ.get("TEST_STUBBORN") == kind:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    started = time.monotonic()
    while True:
        if os.environ.get("TEST_LATE_FAIL") == kind and time.monotonic() - started > 0.8: sys.exit(9)
        time.sleep(0.02)
'''


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recorder-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.output = self.root / "output with spaces"
        self.events = self.root / "events.jsonl"
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        WAYLAND_DISPLAY="test-wayland", TEST_EVENTS=str(self.events))
        for name in ("slurp", "pactl", "pw-record", "wf-recorder", "ffmpeg", "ffprobe", "date"):
            tool = self.bin / name
            tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            tool.chmod(0o755)
        self.processes = []
        self.addCleanup(self.stop_remaining)

    def stop_remaining(self):
        for proc, log in self.processes:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            log.close()

    def run_script(self, *args, extra=None):
        return subprocess.run(["bash", str(SCRIPT), *args], env={**self.env, **(extra or {})},
                              capture_output=True, text=True, timeout=12)

    def start(self, *args, extra=None, default_output=False):
        path = self.root / f"run-{len(self.processes)}.log"
        log = path.open("w")
        command = ["bash", str(SCRIPT)]
        if default_output:
            self.output.mkdir(exist_ok=True)
        else:
            command.extend(["-o", str(self.output)])
        proc = subprocess.Popen([*command, *args], cwd=self.output if default_output else self.root,
                                env={**self.env, **(extra or {})}, stdout=log, stderr=log,
                                start_new_session=True)
        self.processes.append((proc, log))
        return proc, path

    def wait_started(self, proc, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "Recording started" in path.read_text(): return
            if proc.poll() is not None: self.fail(path.read_text())
            time.sleep(0.03)
        self.fail("Timed out waiting for startup: " + path.read_text())

    def finish(self, proc, sig=signal.SIGINT):
        proc.send_signal(sig)
        return proc.wait(timeout=12)

    def sessions(self):
        return sorted(path for path in self.output.glob("conference_*") if path.is_dir())

    def recordings(self):
        return sorted(self.output.glob("conference_*.mp4"))

    def calls(self, name):
        return [json.loads(line) for line in self.events.read_text().splitlines()
                if json.loads(line)["tool"] == name]

    def assert_sources(self, session, present):
        for name in ("video.mp4", "system.wav", "mic.wav"):
            self.assertEqual((session / name).exists(), present, name)

    def assert_captures_stopped(self):
        for name in ("wf-recorder", "pw-record"):
            for event in self.calls(name):
                with self.assertRaises(ProcessLookupError): os.kill(event["pid"], 0)

    def test_help_without_dependencies(self):
        result = subprocess.run(
            [shutil.which("bash"), str(SCRIPT), "--help"], env={**self.env, "PATH": "/nonexistent"},
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertFalse(self.events.exists())

    def test_invalid_arguments(self):
        for args in (("--unknown",), ("--crf",), ("--crf", "52"), ("--crf", "oops"),
                     ("--preset", "oops"), ("--mic-offset", "1.5"),
                     ("--mic-offset", "600001"), ("--system-offset", "-600001"),
                     ("--output-dir", "")):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_script(*args).returncode, 0)
        self.assertFalse(self.events.exists())

    def test_missing_dependencies(self):
        result = subprocess.run([shutil.which("bash"), str(SCRIPT)],
                                env={**self.env, "PATH": "/nonexistent"},
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing dependencies:", result.stderr)

    def test_audio_query_failure(self):
        result = self.run_script(extra={"TEST_PACTL_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not query", result.stderr)
        self.assertEqual(self.calls("slurp"), [])

    def test_exact_device_match(self):
        result = self.run_script(extra={"TEST_NO_EXACT_SINK": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output was not found", result.stderr)

    def test_output_monitor_is_not_used_as_microphone(self):
        result = self.run_script(extra={"TEST_MONITOR_SOURCE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Default input is an output monitor", result.stderr)
        self.assertEqual(self.calls("slurp"), [])

    def test_cancel_creates_no_session(self):
        result = self.run_script("-o", str(self.output), extra={"TEST_CANCEL": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.calls("pw-record"), [])

    def test_success_for_each_stop_signal(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                proc, path = self.start()
                self.wait_started(proc, path)
                self.assertEqual(self.finish(proc, sig), 0, path.read_text())
                self.assertIn("Recording complete", path.read_text())
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.recordings()), 3)
        self.assertTrue((self.output / "conference_2026-10-06_14-30-00.mp4").exists())
        self.assertTrue((self.output / "conference_2026-10-06_14-30-00_1.mp4").exists())
        self.assertTrue((self.output / "conference_2026-10-06_14-30-00_2.mp4").exists())
        self.assert_captures_stopped()
        audio = self.calls("pw-record")
        self.assertEqual(audio[0]["args"][1], "alsa_output.test+sink.1")
        self.assertIn('"stream.capture.sink":true', " ".join(audio[0]["args"]))
        self.assertEqual(audio[1]["args"][1], "alsa_input.test+mic.1")

    def test_quality_and_offsets(self):
        proc, path = self.start("--crf", "20", "--preset", "veryfast",
                                "--system-offset", "0120", "--mic-offset", "-0080")
        self.wait_started(proc, path)
        self.assertEqual(self.finish(proc), 0, path.read_text())
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.recordings()), 1)
        video_args = self.calls("wf-recorder")[0]["args"]
        self.assertIn("crf=20", video_args)
        self.assertIn("preset=veryfast", video_args)
        merge = self.calls("ffmpeg")[0]["args"]
        graph = merge[merge.index("-filter_complex") + 1]
        self.assertIn("adelay=120:all=1", graph)
        self.assertIn("atrim=start=0.080", graph)
        self.assertIn("apad,atrim=duration=2.000", graph)
        self.assertIn("-n", merge)
        self.assertNotIn("-y", merge)

    def test_startup_failure_preserves_sources_and_stops_peers(self):
        for kind in ("video", "system", "mic"):
            with self.subTest(kind=kind):
                proc, path = self.start(extra={"TEST_CAPTURE_FAIL": kind})
                self.assertNotEqual(proc.wait(timeout=12), 0)
                self.assertIn("stopped unexpectedly", path.read_text())
        self.assertEqual(self.calls("ffmpeg"), [])
        self.assert_captures_stopped()

    def test_runtime_failure(self):
        proc, path = self.start(extra={"TEST_LATE_FAIL": "mic"})
        self.wait_started(proc, path)
        self.assertNotEqual(proc.wait(timeout=12), 0)
        self.assert_sources(self.sessions()[0], True)
        self.assertIn("Microphone capture stopped unexpectedly", path.read_text())
        self.assert_captures_stopped()

    def test_merge_failure_preserves_sources(self):
        proc, path = self.start(extra={"TEST_MERGE_FAIL": "1"})
        self.wait_started(proc, path)
        self.assertNotEqual(self.finish(proc), 0)
        self.assert_sources(self.sessions()[0], True)
        self.assertIn("sources are retained", path.read_text())
        self.assert_captures_stopped()

    def test_probe_failure_preserves_sources(self):
        proc, path = self.start(extra={"TEST_DURATION": "N/A"})
        self.wait_started(proc, path)
        self.assertNotEqual(self.finish(proc), 0)
        self.assert_sources(self.sessions()[0], True)
        self.assertEqual(self.calls("ffmpeg"), [])

    def test_empty_source_prevents_merge(self):
        proc, path = self.start(extra={"TEST_EMPTY": "system"})
        self.wait_started(proc, path)
        self.assertNotEqual(self.finish(proc), 0)
        self.assertIn("Missing or empty source file", path.read_text())
        self.assertEqual(self.calls("ffmpeg"), [])

    def test_concurrent_sessions_do_not_overwrite(self):
        first, first_log = self.start()
        second, second_log = self.start()
        self.wait_started(first, first_log)
        self.wait_started(second, second_log)
        self.assertEqual(self.finish(first), 0, first_log.read_text())
        self.assertEqual(self.finish(second), 0, second_log.read_text())
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.recordings()), 2)
        self.assert_captures_stopped()

    def test_default_output_is_current_directory_and_leaves_only_mp4(self):
        proc, path = self.start(default_output=True)
        self.wait_started(proc, path)
        self.assertEqual(self.finish(proc), 0, path.read_text())
        self.assertEqual([item.name for item in self.output.iterdir()],
                         ["conference_2026-10-06_14-30-00.mp4"])

    def test_publish_failure_retains_working_directory(self):
        tool = self.bin / "ln"
        tool.write_text(f"#!{sys.executable}\nimport sys\nprint('simulated publication failure', file=sys.stderr)\nsys.exit(1)\n")
        tool.chmod(0o755)
        proc, path = self.start()
        self.wait_started(proc, path)
        self.assertNotEqual(self.finish(proc), 0)
        self.assert_sources(self.sessions()[0], True)
        self.assertTrue((self.sessions()[0] / "recording.mp4").exists())
        self.assertEqual(self.recordings(), [])
        self.assertIn("Could not save the final MP4", path.read_text())

    def test_existing_recording_is_preserved(self):
        self.output.mkdir()
        existing = self.output / "conference_2026-10-06_14-30-00.mp4"
        existing.write_bytes(b"previous recording")
        proc, path = self.start()
        self.wait_started(proc, path)
        self.assertEqual(self.finish(proc), 0, path.read_text())
        self.assertEqual(existing.read_bytes(), b"previous recording")
        self.assertTrue((self.output / "conference_2026-10-06_14-30-00_1.mp4").exists())
        self.assertEqual(self.sessions(), [])

    def test_forced_shutdown_preserves_sources(self):
        proc, path = self.start(extra={"TEST_STUBBORN": "mic"})
        self.wait_started(proc, path)
        self.assertNotEqual(self.finish(proc), 0)
        self.assert_sources(self.sessions()[0], True)
        self.assertIn("did not stop gracefully", path.read_text())
        self.assertEqual(self.calls("ffmpeg"), [])
        self.assert_captures_stopped()

    @unittest.skipUnless(REAL_FFMPEG, "FFmpeg not installed")
    def test_real_merge_with_synthetic_media(self):
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        def generate(*args):
            subprocess.run([REAL_FFMPEG, "-v", "error", "-nostdin", *args], check=True, timeout=15)
        generate("-f", "lavfi", "-i", "color=c=black:s=128x72:r=25:d=2", "-c:v", "libx264", str(fixtures / "video.mp4"))
        generate("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.5", str(fixtures / "system.wav"))
        generate("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1.5", str(fixtures / "mic.wav"))
        for name, real in (("ffmpeg", REAL_FFMPEG), ("ffprobe", REAL_FFPROBE)):
            if real:
                (self.bin / name).unlink()
                (self.bin / name).symlink_to(real)
        for system_offset, mic_offset in (("120", "-80"), ("-120", "80"), ("0", "0")):
            proc, path = self.start("--system-offset", system_offset,
                                    "--mic-offset", mic_offset, extra={"TEST_MEDIA": str(fixtures)})
            self.wait_started(proc, path)
            self.assertEqual(self.finish(proc), 0, path.read_text())
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.recordings()), 3)
        for recording in self.recordings():
            if not REAL_FFPROBE:
                result = subprocess.run([REAL_FFMPEG, "-i", str(recording), "-f", "null", "-"],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Video: h264", result.stderr)
                self.assertIn("Audio: aac", result.stderr)
                self.assertIn("Duration: 00:00:02.00", result.stderr)
                continue
            probe = subprocess.run([REAL_FFPROBE, "-v", "error", "-show_streams", "-of", "json",
                                    str(recording)], check=True, capture_output=True, text=True)
            streams = json.loads(probe.stdout)["streams"]
            self.assertEqual([stream["codec_name"] for stream in streams], ["h264", "aac"])
            for stream in streams:
                self.assertAlmostEqual(float(stream["duration"]), 2.0, delta=0.05)
            generate("-i", str(recording), "-f", "null", "-")


if __name__ == "__main__":
    unittest.main()
