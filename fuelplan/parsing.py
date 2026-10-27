"""Small, safe parsers for numbers that arrive as header text."""


def whole_number(text: str, *, at_most: int) -> int | None:
    """ASCII decimal text as an int capped at `at_most`; None if it is not a whole number.

    The digits are counted before `int()`: Python refuses to convert more than 4,300
    digits, so a huge header value would otherwise raise instead of being capped.
    """
    if not (text.isascii() and text.isdecimal()):  # also refuses "²", "-1" and "1.5"
        return None
    significant = text.lstrip("0") or "0"
    if len(significant) > len(str(at_most)):
        return at_most
    return min(int(significant), at_most)
