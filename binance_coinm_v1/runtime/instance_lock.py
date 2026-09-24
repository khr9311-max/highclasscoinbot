"""OS-owned lock: released automatically even after a process is killed."""

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        if self._file is not None:
            raise RuntimeError("This bot already owns its instance lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError(f"동일 상태 DB를 사용하는 봇이 이미 실행 중입니다: {self.path}") from exc
        self._file = stream

    def release(self):
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
