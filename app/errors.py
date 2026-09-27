class CommandFailed(Exception):
    """An expected, explainable failure of a CLI command.

    The CLI logs the message (no traceback) and exits non-zero, so the reason is the first
    thing you see in the task's logs.
    """
