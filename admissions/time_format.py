import re
from datetime import time


_TIME_PATTERN = re.compile(r"^(\d{1,2}):([0-5]\d)(?::[0-5]\d(?:\.\d+)?)?$")


def format_time_12_hour(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, time):
        parsed_time = value
    else:
        text = str(value).strip()
        match = _TIME_PATTERN.fullmatch(text)
        if not match or int(match.group(1)) > 23:
            return text
        parsed_time = time(int(match.group(1)), int(match.group(2)))
    return parsed_time.strftime("%I:%M %p").lstrip("0")
