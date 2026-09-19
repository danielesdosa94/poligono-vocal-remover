"""
Motor daemon tests: a real motor.py subprocess driven over stdin/stdout,
CPU only, synthetic fixtures, no Electron.

Run from the repo root:  python -m pytest tests -q
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import soundfile as sf

from utils.protocol import PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
MOTOR = REPO_ROOT / "python" / "motor.py"
TEMP_ROOT = Path(tempfile.gettempdir()) / "poligono-ai-hub"

READY_TIMEOUT = 120.0
JOB_TIMEOUT = 600.0  # htdemucs on CPU for a 5 s clip; generous for slow machines


class MotorClient:
    """Minimal stdin/stdout driver mirroring what Electron's ProcessManager does."""

    def __init__(self, parent_pid=None, extra_args=()):
        args = [sys.executable, "-u", str(MOTOR)]
        if parent_pid is not None:
            args += ["--parent-pid", str(parent_pid)]
        args += list(extra_args)
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.events = []
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def send(self, command: dict) -> None:
        self.proc.stdin.write(json.dumps(command) + "\n")
        self.proc.stdin.flush()

    def next_event(self, timeout: float) -> dict:
        line = self._lines.get(timeout=timeout)
        if line is None:
            raise EOFError("motor stdout closed")
        event = json.loads(line)
        self.events.append(event)
        return event

    def wait_for(self, names, timeout: float, job_id=None) -> dict:
        """Return the first event whose name is in `names` (and jobId matches, if given)."""
        if isinstance(names, str):
            names = (names,)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no {names} within {timeout}s; last events: {self.events[-5:]}")
            event = self.next_event(remaining)
            if event["event"] in names and (job_id is None or event.get("jobId") == job_id):
                return event

    def close(self, timeout: float = 15.0) -> int:
        if self.proc.poll() is None:
            try:
                self.send({"type": "shutdown"})
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdin.close()
        return self.proc.returncode

    def stderr_tail(self) -> str:
        try:
            return self.proc.stderr.read()[-3000:]
        except (OSError, ValueError):
            return ""


@pytest.fixture(scope="module")
def motor():
    """One daemon for the whole module so the model loads once (like the app)."""
    client = MotorClient(parent_pid=os.getpid())
    ready = client.wait_for("ready", READY_TIMEOUT)
    assert ready["protocolVersion"] == PROTOCOL_VERSION
    assert ready["jobId"] is None
    # The ready event carries the full tables, not just names: the UI labels
    # presets with their relative cost and builds bit depth choices from them.
    assert ready["presets"]["hq"]["relativeCost"] == 4
    assert 32 in ready["formats"]["wav"]["bitDepths"]
    assert ready["chunking"]["chunkMinutes"] > 0
    yield client
    code = client.close()
    assert code == 0, client.stderr_tail()


def _separate(job_id: str, input_path: str, outdir: str, **overrides) -> dict:
    command = {
        "type": "separate",
        "jobId": job_id,
        "input": input_path,
        "outputDir": outdir,
        "mode": "vocal_remover",
        "preset": "fast",
        "format": "wav",
        "device": "cpu",
    }
    command.update(overrides)
    return command


# =============================================================================
# Session commands
# =============================================================================

def test_ping_pong_and_bad_commands(motor):
    motor.send({"type": "ping"})
    assert motor.wait_for("pong", 5)["jobId"] is None

    motor.proc.stdin.write("this is not json\n")
    motor.proc.stdin.flush()
    err = motor.wait_for("error", 5)
    assert err["code"] == "BAD_COMMAND" and err["fatal"] is False

    motor.send({"type": "nope"})
    assert motor.wait_for("error", 5)["code"] == "BAD_COMMAND"

    motor.send({"type": "cancel", "jobId": "missing"})
    assert motor.wait_for("warning", 5)["code"] == "UNKNOWN_JOB"

    motor.send({"type": "separate", "jobId": "j-missing", "input": r"C:\does\not\exist.wav"})
    err = motor.wait_for("error", 5, job_id="j-missing")
    assert err["code"] == "FILE_NOT_FOUND" and err["fatal"] is True


# =============================================================================
# Jobs
# =============================================================================

def test_separate_job_reports_success_and_reuses_model(motor, make_fixture, tmp_path):
    path, data, sr = make_fixture(48000, seconds=5.0)
    outdir = tmp_path / "out"

    motor.send(_separate("job-a", path, str(outdir)))
    start = motor.wait_for("start", 10, job_id="job-a")
    assert start["device"] == "cpu" and start["fileType"] == "audio"

    done = motor.wait_for(("success", "error", "cancelled"), JOB_TIMEOUT, job_id="job-a")
    assert done["event"] == "success", done
    assert set(done["outputs"]) == {"vocals", "instrumental"}
    assert done["outputDir"] == str(outdir / Path(path).stem)
    assert done["stats"]["sampleRate"] == sr
    assert done["stats"]["sourceFrames"] == data.shape[0]
    assert done["stats"]["frameDeltas"] == {"vocals": 0, "instrumental": 0}
    for stem_path in done["outputs"].values():
        info = sf.info(stem_path)
        assert info.samplerate == sr and info.frames == data.shape[0] and info.subtype == "FLOAT"

    # Every event of the job carried its id, progress never went backwards,
    # and the steps appeared in pipeline order.
    job_events = [e for e in motor.events if e.get("jobId") == "job-a"]
    assert all(e["jobId"] == "job-a" for e in job_events)
    percents = [e["globalPercent"] for e in job_events if e["event"] == "progress"]
    assert percents == sorted(percents) and percents[-1] == 100.0
    steps = [e["stepNumber"] for e in job_events if e["event"] == "step_change"]
    assert steps == sorted(steps) and 4 in steps

    # Second job on the same daemon: the model must come from the cache.
    motor.send(_separate("job-b", path, str(tmp_path / "out2")))
    done_b = motor.wait_for(("success", "error", "cancelled"), JOB_TIMEOUT, job_id="job-b")
    assert done_b["event"] == "success", done_b

    assert done["stats"]["modelLoadedNow"] is True
    assert done_b["stats"]["modelLoadedNow"] is False
    loaded_logs = [
        e for e in motor.events
        if e["event"] == "log" and e["message"].startswith("Model loaded:")
    ]
    assert len(loaded_logs) == 1

    assert not (TEMP_ROOT / f"motor-{motor.proc.pid}-job-a").exists()
    assert not (TEMP_ROOT / f"motor-{motor.proc.pid}-job-b").exists()


def test_bit_depth_and_chunking_reach_the_engine(motor, make_fixture, tmp_path):
    """The settings added in phase 4 have to survive the trip over stdin."""
    path, data, sr = make_fixture(48000, seconds=6.0)
    outdir = tmp_path / "depth"

    motor.send(_separate(
        "job-depth", path, str(outdir),
        bitDepth=24,
        # 5 s blocks over a 6 s clip: two blocks and one seam. A block has to
        # clear the 2 s crossfade twice over or the layout refuses to split,
        # and the threshold has to come down or a 6 s clip is never a
        # "long file" in the first place.
        chunkMinutes=5.0 / 60.0,
        chunkThresholdMinutes=0,
    ))
    done = motor.wait_for(("success", "error", "cancelled"), JOB_TIMEOUT, job_id="job-depth")

    assert done["event"] == "success", done
    assert done["stats"]["bitDepth"] == 24
    assert done["stats"]["chunks"] > 1
    for stem_path in done["outputs"].values():
        info = sf.info(stem_path)
        assert info.subtype == "PCM_24"
        assert info.samplerate == sr and info.frames == data.shape[0]

    assert not (TEMP_ROOT / f"motor-{motor.proc.pid}-job-depth").exists()


def test_an_impossible_bit_depth_is_rejected_not_silently_changed(motor, make_fixture, tmp_path):
    path, _, _ = make_fixture(44100, seconds=1.0)
    motor.send(_separate("job-bad-depth", path, str(tmp_path / "bad"), format="flac", bitDepth=32))
    err = motor.wait_for("error", 30, job_id="job-bad-depth")
    assert err["code"] == "INVALID_ARGS"
    assert "32" in err["message"]


def test_cancel_running_job_is_cooperative_and_leaves_nothing(motor, make_fixture, tmp_path):
    # 12 s -> at least two segments on htdemucs, so a cancel sent after the
    # first "separating" progress event lands inside the Demucs callback.
    path, _, _ = make_fixture(44100, seconds=12.0, name="long")
    outdir = tmp_path / "out"

    motor.send(_separate("job-cancel", path, str(outdir)))
    while True:
        event = motor.wait_for("progress", JOB_TIMEOUT, job_id="job-cancel")
        if event.get("detail") == "separating" and event["globalPercent"] > 5:
            break

    t0 = time.monotonic()
    motor.send({"type": "cancel", "jobId": "job-cancel"})
    done = motor.wait_for(("success", "error", "cancelled"), 60, job_id="job-cancel")
    elapsed = time.monotonic() - t0
    assert done["event"] == "cancelled", done
    # One CPU segment of htdemucs on a 12 s clip; well under the 15 s Electron
    # gives before taskkill.
    assert elapsed < 15, f"cancel took {elapsed:.1f}s"

    assert not (outdir / "long_44100").exists()
    assert not outdir.exists() or not any(outdir.rglob("*"))
    assert not (TEMP_ROOT / f"motor-{motor.proc.pid}-job-cancel").exists()

    # The daemon is still alive and usable afterwards.
    motor.send({"type": "ping"})
    motor.wait_for("pong", 5)


def test_cancel_queued_job_is_reported_immediately(motor, make_fixture, tmp_path):
    path, _, _ = make_fixture(44100, seconds=5.0)
    motor.send(_separate("job-run", path, str(tmp_path / "a")))
    motor.send(_separate("job-queued", path, str(tmp_path / "b")))
    motor.wait_for("start", 10, job_id="job-run")

    motor.send({"type": "cancel", "jobId": "job-queued"})
    queued = motor.wait_for("cancelled", 5, job_id="job-queued")
    assert queued["reason"] == "Cancelled before start"

    done = motor.wait_for(("success", "error", "cancelled"), JOB_TIMEOUT, job_id="job-run")
    assert done["event"] == "success", done
    assert not (tmp_path / "b").exists()


# =============================================================================
# Lifecycle
# =============================================================================

def test_shutdown_command_exits_cleanly():
    client = MotorClient(parent_pid=os.getpid())
    client.wait_for("ready", READY_TIMEOUT)
    client.send({"type": "shutdown"})
    client.proc.wait(timeout=15)
    assert client.proc.returncode == 0, client.stderr_tail()


def test_stdin_eof_exits_cleanly():
    client = MotorClient(parent_pid=os.getpid())
    client.wait_for("ready", READY_TIMEOUT)
    client.proc.stdin.close()
    client.proc.wait(timeout=15)
    assert client.proc.returncode == 0, client.stderr_tail()


def test_watchdog_exits_when_parent_dies():
    """The motor is told a short-lived process is its parent; it must follow it."""
    fake_parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    client = MotorClient(parent_pid=fake_parent.pid)
    try:
        client.wait_for("ready", READY_TIMEOUT)
        fake_parent.kill()
        fake_parent.wait()
        t0 = time.monotonic()
        client.proc.wait(timeout=10)
        assert time.monotonic() - t0 < 5, "motor did not exit within 5 s of its parent dying"
    finally:
        if fake_parent.poll() is None:
            fake_parent.kill()
        if client.proc.poll() is None:
            client.proc.kill()


def test_stale_temp_dirs_from_dead_motor_are_swept():
    """A folder left by a killed motor is removed on the next start; a live one is kept."""
    dead_pid = 4_000_000  # Above Windows' practical pid range; never alive.
    stale = TEMP_ROOT / f"motor-{dead_pid}-old-job"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "leftover.wav").write_bytes(b"x")
    live = TEMP_ROOT / f"motor-{os.getpid()}-live-job"
    live.mkdir(parents=True, exist_ok=True)
    try:
        client = MotorClient(parent_pid=os.getpid())
        client.wait_for("ready", READY_TIMEOUT)
        assert not stale.exists()
        assert live.exists()
        assert client.close() == 0
    finally:
        for folder in (stale, live):
            if folder.exists():
                for child in folder.iterdir():
                    child.unlink()
                folder.rmdir()
