import threading

_lock = threading.Lock()
_in_progress = False


def set_transfer_in_progress(val: bool):
    global _in_progress
    with _lock:
        _in_progress = val


def is_transfer_in_progress() -> bool:
    with _lock:
        return _in_progress
