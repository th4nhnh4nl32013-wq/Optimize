"""
optrand - random number module for Optimize

    library optrand

    display randint(1, 6)        ! random int between 1 and 6, inclusive
    display random_float()       ! random float in [0.0, 1.0)
    display choice([1, 2, 3])    ! random element from a list
    list a = [1, 2, 3, 4, 5]
    shuffle(a)                   ! shuffles 'a' in place
    display a
    seed(42)                     ! make randomness reproducible
"""

import random as _random


def randint(low, high):
    """Random integer, inclusive of both endpoints (like C++'s
    typical [low, high] convention, not Python's exclusive-high)."""
    return _random.randint(low, high)


def random_float():
    """Random float in [0.0, 1.0)."""
    return _random.random()


def uniform(low, high):
    """Random float in [low, high]."""
    return _random.uniform(low, high)


def choice(lst):
    if not isinstance(lst, list):
        raise TypeError("choice() requires a list.")
    if not lst:
        raise ValueError("choice() on an empty list.")
    return _random.choice(lst)


def shuffle(lst):
    """Shuffles the list IN PLACE (same list object the caller passed in)."""
    if not isinstance(lst, list):
        raise TypeError("shuffle() requires a list.")
    _random.shuffle(lst)
    return lst


def sample(lst, k):
    """Returns a NEW list of k unique random elements from lst."""
    if not isinstance(lst, list):
        raise TypeError("sample() requires a list.")
    return _random.sample(lst, k)


def seed(value):
    """Seed the random generator for reproducible results."""
    _random.seed(value)