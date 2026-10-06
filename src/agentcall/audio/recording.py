"""Private stereo recordings of received PCM and audio actually sent to SCO."""

import os
import tempfile
import time
import wave
from pathlib import Path
from uuid import UUID

import numpy as np


def recording_path(store, task_id):
    UUID(task_id)  # Never derive a filename from an arbitrary route parameter.
    return store.recordings_dir / (task_id + ".wav") if store.recordings_dir else None


class CallRecording:
    def __init__(self, path, rate):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.rate = rate
        self.started = time.monotonic()
        self.tracks = []
        try:
            for _ in range(2):
                self.tracks.append(tempfile.TemporaryFile(dir=self.path.parent))
        except OSError:
            for track in self.tracks:
                track.close()
            raise
        self.positions = [0, 0]
        self.failed = False

    def write(self, channel, pcm, *, output_end=None):
        if self.failed or not pcm:
            return
        try:
            samples = len(pcm) // 2
            elapsed = (output_end if output_end is not None else time.monotonic()) - self.started
            position = max(self.positions[channel], round(elapsed * self.rate) - samples, 0)
            if position + samples > self.rate * 3610:
                raise ValueError("recording exceeds maximum call duration")
            track = self.tracks[channel]
            if track.tell() != position * 2:
                track.seek(position * 2)
            track.write(pcm)
            self.positions[channel] = position + samples
        except (OSError, ValueError):
            # Recording failure must not stop a telephone conversation.
            self.failed = True

    def finish(self):
        temporary = None
        try:
            if self.failed or not max(self.positions):
                return None
            fd, temporary = tempfile.mkstemp(prefix=".recording-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(self.rate)
                for track in self.tracks:
                    track.seek(0)
                for start in range(0, max(self.positions), self.rate):
                    count = min(self.rate, max(self.positions) - start)
                    stereo = np.zeros((count, 2), dtype="<i2")
                    for channel, track in enumerate(self.tracks):
                        data = track.read(count * 2)
                        stereo[: len(data) // 2, channel] = np.frombuffer(data, dtype="<i2")
                    output.writeframesraw(stereo.tobytes())
            os.replace(temporary, self.path)
            return self.path
        except (OSError, ValueError):
            self.failed = True
            return None
        finally:
            for track in self.tracks:
                track.close()
            if temporary:
                Path(temporary).unlink(missing_ok=True)
