"""
algorithm - C++ <algorithm>-style helpers for Optimize

C++ algorithms operate on iterator ranges [begin, end) into a
container. Optimize only has plain lists and integer indices, so
this module uses the same idea with a twist: you tell it which list
to work on once with set_container(lst), and afterwards every
function takes (begin, end) index positions into THAT list, with
'end' EXCLUSIVE - exactly like C++'s v.end().

    library algorithm

    list a = [5, 3, 1, 4, 2]
    set_container(a)

    sort(0, len(a))            ! a is now [1, 2, 3, 4, 5], sorted in place
    display a

    display min_element(0, len(a))   ! -> 1
    display max_element(0, len(a))   ! -> 5
    display count(0, len(a), 3)      ! -> 1
    display find(0, len(a), 4)       ! -> index of 4, or -1 if absent

Because Optimize lists are passed by reference, mutating algorithms
(sort, reverse, fill, rotate, unique) change the SAME list object
you passed to set_container - so the .opt-side variable is updated
too, just like a real C++ container would be.
"""

_container = None


def set_container(lst):
    """Point every algorithm() call at this list until changed."""
    global _container
    if not isinstance(lst, list):
        raise TypeError("set_container() requires a list.")
    _container = lst
    return lst


def _require_container():
    if _container is None:
        raise RuntimeError(
            "No container set. Call set_container(your_list) first.")
    return _container


def _check_range(begin, end, length):
    if begin < 0 or end < begin or end > length:
        raise IndexError(
            f"Invalid range [{begin}, {end}) for container of length {length}.")


# ── non-mutating ─────────────────────────────

def min_element(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    if begin == end:
        raise ValueError("min_element() on an empty range.")
    return min(lst[begin:end])


def max_element(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    if begin == end:
        raise ValueError("max_element() on an empty range.")
    return max(lst[begin:end])


def count(begin, end, value):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    return lst[begin:end].count(value)


def find(begin, end, value):
    """Returns the absolute index of the first match in [begin, end),
    or -1 if not found (Optimize has no end-iterator sentinel)."""
    lst = _require_container()
    _check_range(begin, end, len(lst))
    for i in range(begin, end):
        if lst[i] == value:
            return i
    return -1


def accumulate(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    total = 0
    for i in range(begin, end):
        total += lst[i]
    return total


def is_sorted(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    sub = lst[begin:end]
    return sub == sorted(sub)


# ── mutating (operate in place on the same list object) ─────────

def sort(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    lst[begin:end] = sorted(lst[begin:end])
    return lst


def sort_desc(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    lst[begin:end] = sorted(lst[begin:end], reverse=True)
    return lst


def reverse(begin, end):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    lst[begin:end] = list(reversed(lst[begin:end]))
    return lst


def fill(begin, end, value):
    lst = _require_container()
    _check_range(begin, end, len(lst))
    for i in range(begin, end):
        lst[i] = value
    return lst


def rotate(begin, mid, end):
    """Like std::rotate: the element at 'mid' becomes the new first
    element of the range [begin, end)."""
    lst = _require_container()
    _check_range(begin, end, len(lst))
    if mid < begin or mid > end:
        raise IndexError(f"'mid' ({mid}) must be within [{begin}, {end}].")
    segment = lst[begin:end]
    pivot = mid - begin
    lst[begin:end] = segment[pivot:] + segment[:pivot]
    return lst


def unique(begin, end):
    """Like std::unique: collapses CONSECUTIVE equal elements within
    [begin, end) in place, and returns the new logical end index.
    The list is not shrunk (same as C++) - elements at/after the
    returned index are left-over and should be ignored."""
    lst = _require_container()
    _check_range(begin, end, len(lst))
    segment = lst[begin:end]
    deduped = []
    for item in segment:
        if not deduped or deduped[-1] != item:
            deduped.append(item)
    new_end = begin + len(deduped)
    lst[begin:new_end] = deduped
    return new_end


def swap(i, j):
    lst = _require_container()
    n = len(lst)
    if i < 0 or i >= n or j < 0 or j >= n:
        raise IndexError(f"Index out of range for swap({i}, {j}).")
    lst[i], lst[j] = lst[j], lst[i]
    return lst