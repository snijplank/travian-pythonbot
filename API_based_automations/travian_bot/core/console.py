import threading

# Single global console lock to avoid interleaved prints between threads
CONSOLE_LOCK = threading.Lock()

# Track whether a status/progress line is currently rendered without a trailing newline
_STATUS_ACTIVE = False

def print_line(msg: str) -> None:
    """Print a normal line. If a status line is active, end it first."""
    global _STATUS_ACTIVE
    try:
        with CONSOLE_LOCK:
            if _STATUS_ACTIVE:
                try:
                    print()
                except Exception:
                    pass
                _STATUS_ACTIVE = False
            print(msg, flush=True)
    except Exception:
        # Best-effort; never crash on logging
        try:
            print(msg)
        except Exception:
            pass

def print_status(msg: str) -> None:
    """Render/refresh a single-line status (e.g., progress bar) without newline."""
    global _STATUS_ACTIVE
    try:
        with CONSOLE_LOCK:
            _STATUS_ACTIVE = True
            print("\r" + msg, end="", flush=True)
    except Exception:
        # Fallback to normal line if status printing fails
        print_line(msg)

def end_status() -> None:
    """Terminate the active status line with a newline, if any."""
    global _STATUS_ACTIVE
    try:
        with CONSOLE_LOCK:
            if _STATUS_ACTIVE:
                print()
                _STATUS_ACTIVE = False
    except Exception:
        pass
