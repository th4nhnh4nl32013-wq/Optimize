"""
optmath - math module for Optimize

Usage from a .opt file:
    library optmath

    display pi
    display e
    display power(2, 10)
    display sqrt(16)
    display log(8, 2)
    display sin(0)
    display fraction(1, 3)
"""

import math as _math
from fractions import Fraction as _Fraction

_builtin_min = min
_builtin_max = max

# ── constants ────────────────────────────────
pi = _math.pi
e  = _math.e

# ── min / max ────────────────────────────────
# Optimize already has Python's built-in min/max for two-or-more
# plain arguments. These wrappers also accept a single list, since
# Optimize lists are passed around as Python lists.

def min_(*args):
    if len(args) == 1 and isinstance(args[0], list):
        return _builtin_min(args[0])
    return _builtin_min(args)

def max_(*args):
    if len(args) == 1 and isinstance(args[0], list):
        return _builtin_max(args[0])
    return _builtin_max(args)

# expose as 'min'/'max' inside Optimize without shadowing Python's
# own builtins on this side
min = min_
max = max_

# ── power / sqrt / log ───────────────────────

def power(base, exponent):
    return base ** exponent

def sqrt(x):
    if x < 0:
        raise ValueError("sqrt() of a negative number is not supported.")
    return _math.sqrt(x)

def log(x, base=_math.e):
    if x <= 0:
        raise ValueError("log() requires a positive number.")
    if base == _math.e:
        return _math.log(x)
    return _math.log(x, base)

def log10(x):
    return _math.log10(x)

def log2(x):
    return _math.log2(x)

# ── trig (radians, matching standard math conventions) ───────────

def sin(x):
    return _math.sin(x)

def cos(x):
    return _math.cos(x)

def tan(x):
    return _math.tan(x)

def asin(x):
    return _math.asin(x)

def acos(x):
    return _math.acos(x)

def atan(x):
    return _math.atan(x)

def degrees(x):
    return _math.degrees(x)

def radians(x):
    return _math.radians(x)

# ── fractions ─────────────────────────────────
# Optimize has no native fraction type, so a fraction is represented
# as a 2-element list [numerator, denominator] in lowest terms.

def fraction(numerator, denominator):
    f = _Fraction(numerator, denominator)
    return [f.numerator, f.denominator]

def fraction_add(frac_a, frac_b):
    a = _Fraction(frac_a[0], frac_a[1])
    b = _Fraction(frac_b[0], frac_b[1])
    r = a + b
    return [r.numerator, r.denominator]

def fraction_sub(frac_a, frac_b):
    a = _Fraction(frac_a[0], frac_a[1])
    b = _Fraction(frac_b[0], frac_b[1])
    r = a - b
    return [r.numerator, r.denominator]

def fraction_mul(frac_a, frac_b):
    a = _Fraction(frac_a[0], frac_a[1])
    b = _Fraction(frac_b[0], frac_b[1])
    r = a * b
    return [r.numerator, r.denominator]

def fraction_div(frac_a, frac_b):
    a = _Fraction(frac_a[0], frac_a[1])
    b = _Fraction(frac_b[0], frac_b[1])
    r = a / b
    return [r.numerator, r.denominator]

def fraction_to_float(frac):
    return frac[0] / frac[1]

# ── misc ──────────────────────────────────────

def floor(x):
    return _math.floor(x)

def ceil(x):
    return _math.ceil(x)

def factorial(n):
    return _math.factorial(n)

def gcd(a, b):
    return _math.gcd(a, b)

def lcm(a, b):
    return _math.lcm(a, b)