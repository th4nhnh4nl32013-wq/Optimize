import sys


_state = {
    "stdin": None,
    "stdout": None,
}


def _normalize_mode(mode):
    mode = str(mode).strip().lower()
    if mode in {"stdin", "stdout"}:
        return mode
    raise ValueError("mode must be 'stdin' or 'stdout'")


def open_file(path, mode):
    mode = _normalize_mode(mode)
    if mode == "stdin":
        if _state["stdin"] is not None and not _state["stdin"].closed:
            _state["stdin"].close()
        _state["stdin"] = open(path, "r", encoding="utf-8")
        return _state["stdin"]

    if _state["stdout"] is not None and not _state["stdout"].closed:
        _state["stdout"].close()
    _state["stdout"] = open(path, "w", encoding="utf-8")
    return _state["stdout"]


def read_lines(count=None):
    stream = _state["stdin"] if _state["stdin"] is not None else sys.stdin

    if count is None or count == "-all":
        return stream.read()

    count = int(count)
    if count < 0:
        raise ValueError("line count cannot be negative")

    lines = []
    for _ in range(count):
        line = stream.readline()
        if not line:
            break
        lines.append(line)
    return "".join(lines)


def write_text(text):
    stream = _state["stdout"] if _state["stdout"] is not None else sys.stdout
    stream.write(str(text))
    stream.flush()


def close_all():
    if _state["stdin"] is not None and not _state["stdin"].closed:
        _state["stdin"].close()
    if _state["stdout"] is not None and not _state["stdout"].closed:
        _state["stdout"].close()
    _state["stdin"] = None
    _state["stdout"] = None
