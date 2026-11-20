"""Starter support utilities."""
import sys as _sys

# Windows consoles default to cp1252; game text can contain characters such as '↔'.
# Replace unencodable characters instead of crashing an agent mid-task.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass
