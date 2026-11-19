"""Expose the reply string from an incrementally generated JSON object."""

import json
import re
from collections.abc import Callable

REPLY_PREFIX = re.compile(r'^\s*\{\s*"reply"\s*:\s*"')


class ReplyStream:
    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.buffer = ""
        self.sent = ""
        self.finished = False

    def feed(self, chunk: str) -> None:
        if self.finished or not chunk:
            return
        self.buffer += chunk
        match = REPLY_PREFIX.match(self.buffer)
        if match is None:
            return
        raw = self.buffer[match.end() :]
        safe_end = 0
        position = 0
        while position < len(raw):
            char = raw[position]
            if char == '"':
                self.finished = True
                break
            if char == "\\":
                if position + 1 >= len(raw):
                    break
                if raw[position + 1] == "u":
                    if position + 6 > len(raw):
                        break
                    if not re.fullmatch(r"[0-9a-fA-F]{4}", raw[position + 2 : position + 6]):
                        return
                    position += 6
                else:
                    position += 2
            else:
                position += 1
            safe_end = position
        try:
            decoded = json.loads('"' + raw[:safe_end] + '"')
        except (ValueError, UnicodeError):
            return
        if decoded.startswith(self.sent):
            delta = decoded[len(self.sent) :]
            if delta:
                self.sent = decoded
                self.emit(delta)
