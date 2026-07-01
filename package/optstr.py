"""
optstr - string helper module for Optimize

    library optstr

    display upper("hello")
    display lower("HELLO")
    display trim("  hi  ")
    display split("a,b,c", ",")
    display join(["a", "b", "c"], "-")
    display contains("hello world", "world")
    display replace("hello", "l", "L")
    display starts_with("hello", "he")
    display ends_with("hello", "lo")
    display str_len("hello")
    display substring("hello", 1, 4)
    display repeat("ab", 3)
    display index_of("hello", "l")
    display is_digit("123")
    display is_alpha("abc")
"""


def upper(s):
    return str(s).upper()


def lower(s):
    return str(s).lower()


def trim(s):
    return str(s).strip()


def split(s, sep=" "):
    return str(s).split(sep)


def join(lst, sep=""):
    if not isinstance(lst, list):
        raise TypeError("join() requires a list as its first argument.")
    return sep.join(str(item) for item in lst)


def contains(s, sub):
    return sub in str(s)


def replace(s, old, new):
    return str(s).replace(old, new)


def starts_with(s, prefix):
    return str(s).startswith(prefix)


def ends_with(s, suffix):
    return str(s).endswith(suffix)


def str_len(s):
    return len(str(s))


def substring(s, start, end):
    """Like C++'s s.substr(start, end-start) expressed as a Python
    slice: end is EXCLUSIVE, consistent with the algorithm module."""
    return str(s)[start:end]


def repeat(s, times):
    return str(s) * times


def index_of(s, sub):
    """Returns the index of the first occurrence, or -1 if absent."""
    return str(s).find(sub)


def reverse_str(s):
    return str(s)[::-1]


def is_digit(s):
    return str(s).isdigit()


def is_alpha(s):
    return str(s).isalpha()


def to_upper_first(s):
    s = str(s)
    return s[:1].upper() + s[1:]


def capitalize(s):
    return str(s).capitalize()