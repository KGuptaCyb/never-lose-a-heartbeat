"""Parsing and integrity checks for biosignal-mock replies."""
import json
import zlib


class CorruptReply(Exception):
    pass


def _data_span(raw):
    """Return (start, end) of the top-level "data" value in a raw JSON object.

    The crc32 covers the data text exactly as sent, so we must locate it in the
    raw bytes rather than re-serialising the parsed list.
    """
    depth = 0
    i, n = 0, len(raw)
    key = None
    in_str = False
    str_start = 0
    while i < n:
        c = raw[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
                if depth == 1:
                    key = raw[str_start + 1:i]
            i += 1
            continue
        if c == '"':
            in_str = True
            str_start = i
        elif c in "{[":
            if depth == 1 and c == "[" and key == "data":
                # bracket-match; data holds only numbers so no strings inside
                d, j = 0, i
                while j < n:
                    if raw[j] == "[":
                        d += 1
                    elif raw[j] == "]":
                        d -= 1
                        if d == 0:
                            return i, j + 1
                    j += 1
                raise CorruptReply("unterminated data array")
            depth += 1
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 1:
            key = None
        i += 1
    raise CorruptReply("no data field")


def parse_reply(line):
    """Parse one reply line. Raises CorruptReply if it must not be used."""
    raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    try:
        msg = json.loads(raw)
    except ValueError as e:
        raise CorruptReply(f"bad json: {e}") from None
    if not isinstance(msg, dict) or "data" not in msg or "crc32" not in msg:
        raise CorruptReply("missing fields")
    s, e = _data_span(raw)
    text = raw[s:e]
    crc = format(zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF, "08x")
    if crc != str(msg["crc32"]).lower().zfill(8):
        raise CorruptReply(f"crc mismatch {crc} != {msg['crc32']}")
    data = msg["data"]
    if msg.get("count") is not None and len(data) != msg["count"]:
        raise CorruptReply("count does not match data length")
    width = 3 if msg["sensor"] == 1 else 2
    for row in data:
        if len(row) != width:
            raise CorruptReply("wrong channel count")
    return msg
