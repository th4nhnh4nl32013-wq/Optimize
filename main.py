import sys
import os
import importlib.util
import re as _re

from package.readinout import open_file, read_lines, write_text

# ─────────────────────────────────────────────
#  Exceptions
# ─────────────────────────────────────────────

class OptimizeError(Exception):
    pass

class ReturnException(Exception):
    def __init__(self, value=None):
        self.value = value

class NextException(Exception):   # continue
    pass

class StopException(Exception):   # break
    pass

# ─────────────────────────────────────────────
#  Global state
# ─────────────────────────────────────────────

global_vars  = {}
functions    = {}
classes      = {}   # {class_name: OptClass}
loaded_libs  = set()
lib_exports  = {}   # {lib_name: [list of symbol names it added to global_vars]}
PACKAGE_DIR  = os.path.join(os.path.dirname(__file__), "package")

# ─────────────────────────────────────────────
#  Scope object  (Lua-style local/global)
# ─────────────────────────────────────────────

class Scope:
    __slots__ = ("frames",)

    def __init__(self, frames=None):
        if frames is None:
            self.frames = [global_vars, {}]
        else:
            self.frames = frames

    def child(self):
        return Scope(self.frames + [{}])

    def get(self, name):
        for frame in reversed(self.frames):
            if name in frame:
                return frame[name]
        raise OptimizeError(f"'{name}' is not defined.")

    def has(self, name):
        return any(name in frame for frame in self.frames)

    def set_local(self, name, value):
        self.frames[-1][name] = value

    def set_global(self, name, value):
        global_vars[name] = value

    def set_auto(self, name, value):
        for frame in reversed(self.frames):
            if name in frame:
                frame[name] = value
                return
        global_vars[name] = value

    def delete(self, name):
        for frame in reversed(self.frames):
            if name in frame:
                del frame[name]
                return
        raise OptimizeError(f"Cannot delete '{name}': name is not defined.")

    def as_dict(self):
        merged = {}
        for frame in self.frames:
            merged.update(frame)
        return merged

# ─────────────────────────────────────────────
#  Class / instance objects  (Python-style classes)
# ─────────────────────────────────────────────

class OptClass:
    """A user-defined `class Name` / `class Name (Parent)` block.
    Holds its own methods (dict of name -> function-def dict, same
    shape as entries in `functions`) plus an optional parent class
    for method/attribute inheritance lookups."""
    __slots__ = ("name", "methods", "parent")

    def __init__(self, name, parent=None):
        self.name = name
        self.methods = {}
        self.parent = parent

    def find_method(self, mname):
        cls = self
        while cls is not None:
            if mname in cls.methods:
                return cls.methods[mname]
            cls = cls.parent
        return None

    def is_subclass_of(self, other):
        cls = self
        while cls is not None:
            if cls is other:
                return True
            cls = cls.parent
        return False


class OptInstance:
    """A runtime instance of an OptClass. Attributes live in
    `attrs`, a plain dict, set/read via `self.attr` inside methods
    or `obj.attr` from outside."""
    __slots__ = ("cls", "attrs")

    def __init__(self, cls: OptClass):
        self.cls = cls
        self.attrs = {}

    def get_attr(self, name):
        if name in self.attrs:
            return self.attrs[name]
        method = self.cls.find_method(name)
        if method is not None:
            # Bound method: a callable that injects `self` as the
            # first positional argument automatically.
            return lambda *a, **kw: _call_method(self, method, list(a), kw)
        raise OptimizeError(f"'{self.cls.name}' object has no attribute '{name}'.")

    def has_attr(self, name):
        if name in self.attrs:
            return True
        return self.cls.find_method(name) is not None

    def set_attr(self, name, value):
        self.attrs[name] = value

    def __repr__(self):
        return f"<{self.cls.name} object>"


def _call_method(instance: "OptInstance", method: dict, arg_values: list, kwarg_values: dict):
    """Invokes a resolved method dict (as stored in OptClass.methods)
    on `instance`, automatically binding `self`."""
    fn_scope = _bind_args_for_method(method, instance, arg_values, kwarg_values)
    try:
        execute_block(method["body"], fn_scope)
    except ReturnException as r:
        return r.value
    return None


def _bind_args_for_method(method: dict, instance: "OptInstance", positional: list, keywords: dict) -> "Scope":
    params      = method["params"]
    star_args   = method.get("star_args")
    star_kwargs = method.get("star_kwargs")
    if not params or params[0] != "self":
        raise OptimizeError(
            f"Method '{method.get('name', '?')}' must declare 'self' as its first parameter.")
    fn_scope = Scope()
    fn_scope.set_local("self", instance)

    real_params = params[1:]
    remaining_kw = dict(keywords)

    for i, p in enumerate(real_params):
        if i < len(positional):
            fn_scope.set_local(p, positional[i])
        elif p in remaining_kw:
            fn_scope.set_local(p, remaining_kw.pop(p))
        else:
            raise OptimizeError(f"Method '{method.get('name','?')}' missing argument '{p}'.")

    extra_positional = positional[len(real_params):]
    if extra_positional and star_args is None:
        raise OptimizeError(
            f"Method expects {len(real_params)} positional argument(s), got {len(positional)}.")
    if star_args is not None:
        fn_scope.set_local(star_args, list(extra_positional))

    if star_kwargs is not None:
        fn_scope.set_local(star_kwargs, remaining_kw)
    elif remaining_kw:
        unexpected = ", ".join(remaining_kw.keys())
        raise OptimizeError(f"Method got unexpected keyword argument(s): {unexpected}.")

    return fn_scope


def instantiate_class(cls: "OptClass", positional: list, keywords: dict) -> "OptInstance":
    instance = OptInstance(cls)
    init_method = cls.find_method("__init__")
    if init_method is not None:
        _call_method(instance, init_method, positional, keywords)
    elif positional or keywords:
        raise OptimizeError(
            f"Class '{cls.name}' has no '__init__' but was given arguments.")
    return instance


_ATTR_ACCESS_PATTERN = _re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.([A-Za-z_][A-Za-z0-9_]*)$")

def _split_attr_chain(text: str):
    """For 'self.x' or 'a.b.c' (NOT a call, no trailing parens),
    returns (root_expr, [attr1, attr2, ...]) or None if `text`
    doesn't look like a plain dotted attribute chain."""
    if "(" in text or ")" in text:
        return None
    parts = text.split(".")
    if len(parts) < 2:
        return None
    if not all(p.isidentifier() for p in parts):
        return None
    return parts[0], parts[1:]

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

SAFE_BUILTINS = {
    "True": True, "False": False, "None": None,
    "len": len, "int": int, "float": float,
    "str": str, "abs": abs, "round": round,
    "min": min, "max": max, "sum": sum,
    # reversed() normally returns a lazy iterator; wrap it so
    # 'display reversed(a)' and further indexing both just work.
    "reversed": lambda seq: list(reversed(seq)),
}

def _strip_comment(line: str) -> str:
    in_str = None
    for i, ch in enumerate(line):
        if ch in ('"', "'") and in_str is None:
            in_str = ch
        elif ch == in_str:
            in_str = None
        elif ch == "!" and in_str is None:
            return line[:i].strip()
    return line.strip()

import re as _re

def _replace_last_item(expr: str) -> str:
    # `a[last_item]` means "the last element of a" -> a[-1].
    # Rewritten textually so 'last_item' never needs to be a real
    # bound name (it has no meaning outside of a [...] index).
    return expr.replace("[last_item]", "[-1]")

_START_END_PATTERN = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(start|end)\(\)")

def _replace_start_end(expr: str) -> str:
    # `a.start()` -> 0          (first valid index)
    # `a.end()`   -> len(a)     (one past the last index, exclusive,
    #                            matching the algorithm module's
    #                            (begin, end) convention)
    # Rewritten textually per-match so each occurrence uses ITS OWN
    # variable name, e.g. 'sort(a.start(), a.end())' becomes
    # 'sort(0, len(a))', and a second list 'b.end()' independently
    # becomes 'len(b)'.
    def _sub(m):
        name, which = m.group(1), m.group(2)
        return "0" if which == "start" else f"len({name})"
    return _START_END_PATTERN.sub(_sub, expr)

def safe_eval(expr: str, scope: Scope):
    expr = _replace_last_item(expr)
    expr = _replace_start_end(expr)
    try:
        merged = {**SAFE_BUILTINS, **scope.as_dict()}
        # Make Optimize-defined `function`s callable from inside a
        # larger expression too (e.g. 'add(1, 2) + 3'), not just as
        # a bare standalone call. Scope-bound names still win if a
        # local variable happens to shadow a function name.
        for fname in functions:
            if fname not in merged:
                merged[fname] = (lambda _n: lambda *a, **kw: call_function_with_values(_n, list(a), kw))(fname)
        return eval(expr, {"__builtins__": {}}, merged)
    except ZeroDivisionError:
        raise OptimizeError("Division by zero.")
    except OptimizeError:
        raise
    except Exception as e:
        raise OptimizeError(f"Cannot evaluate '{expr}': {e}")

def extract_parens(text: str) -> str:
    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        raise OptimizeError(f"Condition must be wrapped in parentheses: '{text}'")
    return text[1:-1].strip()

def strip_trailing_colon(text: str) -> str:
    text = text.rstrip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    return text

def split_top_level(inner: str) -> list:
    parts, depth, current = [], 0, ""
    in_str = None
    for ch in inner:
        if in_str is not None:
            current += ch
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch; current += ch
        elif ch == "(" or ch == "[":
            depth += 1; current += ch
        elif ch == ")" or ch == "]":
            depth -= 1; current += ch
        elif ch == "," and depth == 0:
            parts.append(current.strip()); current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts

def extract_function_call(text: str):
    """
    If text is exactly 'funcname(...)' - i.e. the FIRST '(' is
    balanced by the LAST ')' with nothing trailing after it - return
    (funcname, args_string). Otherwise return None.

    This rules out things like 'f(x) + g(y)' or 'f(x) / 2', which
    end with ')' and contain '(' but are NOT a single bare call.
    """
    text = text.strip()
    if "(" not in text or not text.endswith(")"):
        return None
    paren_idx = text.index("(")
    name = text[:paren_idx].strip()
    # Allow dotted call targets too, e.g. 'self.greet()' or
    # 'obj.method()' or 'ClassName()' - only the leading segment
    # needs to be a bare identifier; the rest is dot-separated
    # identifiers, checked again by the caller.
    if not name:
        return None
    if not (name.isidentifier() or all(p.isidentifier() for p in name.split("."))):
        return None
    if name in ("if", "for", "while", "function", "class"):
        return None

    # Walk from paren_idx to confirm this '(' closes exactly at the
    # final character, with quote-awareness so parens inside string
    # literals don't throw off the depth count.
    depth = 0
    in_str = None
    for i in range(paren_idx, len(text)):
        ch = text[i]
        if in_str is not None:
            if ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # The matching close-paren must be the LAST character.
                if i != len(text) - 1:
                    return None
                break
    if depth != 0:
        return None

    args = text[paren_idx+1:-1].strip()
    return (name, args)

# ─────────────────────────────────────────────
#  Function parameter / call-argument parsing
#  (supports *args and **kargs, Python-style)
# ─────────────────────────────────────────────

def parse_params(param_str: str):
    """Parses a function's parameter list, Python-style:
       foo(a, b, *args, **kargs)
       Fixed params must come first, then at most one *name,
       then at most one **name."""
    params, star_args, star_kwargs = [], None, None
    for p in [x.strip() for x in param_str.split(",") if x.strip()]:
        if p.startswith("**"):
            name = p[2:].strip()
            if not name.isidentifier():
                raise OptimizeError(f"Invalid **kargs parameter: '{p}'")
            if star_kwargs is not None:
                raise OptimizeError("Only one **kargs parameter is allowed.")
            star_kwargs = name
        elif p.startswith("*"):
            name = p[1:].strip()
            if not name.isidentifier():
                raise OptimizeError(f"Invalid *args parameter: '{p}'")
            if star_kwargs is not None:
                raise OptimizeError("*args must come before **kargs.")
            if star_args is not None:
                raise OptimizeError("Only one *args parameter is allowed.")
            star_args = name
        else:
            if not p.isidentifier():
                raise OptimizeError(f"Invalid parameter name: '{p}'")
            if star_args is not None or star_kwargs is not None:
                raise OptimizeError("Fixed parameters must come before *args/**kargs.")
            params.append(p)
    return params, star_args, star_kwargs

_KWARG_PATTERN = _re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*(.*)$")

def _parse_call_args(arg_str: str, scope: Scope):
    """Splits a call's argument string into (positional_values, keyword_values),
       supporting trailing name=value args a la Python: foo(1, 2, x=3, y=4)."""
    positional, keywords, seen_kw = [], {}, False
    if not arg_str.strip():
        return positional, keywords
    for raw in split_top_level(arg_str):
        raw = raw.strip()
        m = _KWARG_PATTERN.match(raw)
        if m:
            kname, kexpr = m.group(1), m.group(2)
            if kname in keywords:
                raise OptimizeError(f"Duplicate keyword argument '{kname}'.")
            keywords[kname] = parse_value(kexpr.strip(), scope)
            seen_kw = True
        else:
            if seen_kw:
                raise OptimizeError("Positional arguments cannot follow keyword arguments.")
            positional.append(parse_value(raw, scope))
    return positional, keywords

def _bind_args(name: str, positional: list, keywords: dict) -> "Scope":
    fn = functions[name]
    params      = fn["params"]
    star_args   = fn.get("star_args")
    star_kwargs = fn.get("star_kwargs")
    fn_scope = Scope()
    remaining_kw = dict(keywords)

    for i, p in enumerate(params):
        if i < len(positional):
            fn_scope.set_local(p, positional[i])
        elif p in remaining_kw:
            fn_scope.set_local(p, remaining_kw.pop(p))
        else:
            raise OptimizeError(f"Function '{name}' missing argument '{p}'.")

    extra_positional = positional[len(params):]
    if extra_positional and star_args is None:
        raise OptimizeError(
            f"Function '{name}' expects {len(params)} positional argument(s), got {len(positional)}.")
    if star_args is not None:
        fn_scope.set_local(star_args, list(extra_positional))

    if star_kwargs is not None:
        fn_scope.set_local(star_kwargs, remaining_kw)
    elif remaining_kw:
        unexpected = ", ".join(remaining_kw.keys())
        raise OptimizeError(f"Function '{name}' got unexpected keyword argument(s): {unexpected}.")

    return fn_scope

# ─────────────────────────────────────────────
#  Value parser
# ─────────────────────────────────────────────

def _is_pure_string_literal(raw: str):
    """Returns True only if `raw` is ENTIRELY one quoted string
    literal with nothing else around it - e.g. '"hi"' is, but
    '"hi" + x' merely starts and ends with a quote character and is
    NOT a pure literal (it's a concatenation expression)."""
    if len(raw) < 2:
        return False
    quote = raw[0]
    if quote not in ('"', "'"):
        return False
    if raw[-1] != quote:
        return False
    # Walk the inside; if the same quote character appears again
    # before the final character, the "literal" actually ends
    # earlier and there's more content after it (so it's not pure).
    for ch in raw[1:-1]:
        if ch == quote:
            return False
    return True

def parse_value(raw: str, scope: Scope):
    raw = raw.strip()

    if raw.startswith("format "):
        body = raw[6:].strip()
        if not body:
            raise OptimizeError("format requires a string literal.")
        if not ((body.startswith('"') and body.endswith('"')) or
                (body.startswith("'") and body.endswith("'"))):
            raise OptimizeError("fomat requires a quoted string literal.")
        text = body[1:-1]

        def _sub(match):
            name = match.group(1)
            if not scope.has(name):
                raise OptimizeError(f"'{name}' is not defined.")
            return str(scope.get(name))

        return _re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", _sub, text)

    if _is_pure_string_literal(raw):
        return raw[1:-1]

    if raw == "True":
        return True

    if raw == "False":
        return False

    # Function call
    call_info = extract_function_call(raw)

    if call_info:
        fname, args = call_info

        # Class instantiation: ClassName(...)
        if fname in classes and not scope.has(fname):
            positional, keywords = _parse_call_args(args, scope)
            return instantiate_class(classes[fname], positional, keywords)

        # Dotted call: self.method(...) / obj.method(...) / a.b.method(...)
        if "." in fname:
            target_expr, _, mname = fname.rpartition(".")
            target = parse_value(target_expr, scope)
            if isinstance(target, OptInstance):
                method = target.cls.find_method(mname)
                if method is None:
                    raise OptimizeError(
                        f"'{target.cls.name}' object has no method '{mname}'.")
                positional, keywords = _parse_call_args(args, scope)
                return _call_method(target, method, positional, keywords)
            # Not an OptInstance: fall through to generic handling below
            # (e.g. calling a bound Python method on some other object).
            attr_fn = getattr(target, mname, None) if not isinstance(target, (dict, list)) else None
            if callable(attr_fn):
                positional, keywords = _parse_call_args(args, scope)
                try:
                    return attr_fn(*positional, **keywords)
                except OptimizeError:
                    raise
                except Exception as e:
                    raise OptimizeError(f"{fname}(): {e}")
            raise OptimizeError(f"'{fname}' is not callable.")

        # Optimize function
        if fname in functions:
            return call_function(fname, args, scope)

        # Python function
        if scope.has(fname):
            fn = scope.get(fname)

            if callable(fn):
                arg_values = []
                kwarg_values = {}

                if args.strip():
                    for arg in split_top_level(args):
                        arg = arg.strip()
                        m = _KWARG_PATTERN.match(arg)
                        if m:
                            kwarg_values[m.group(1)] = parse_value(m.group(2).strip(), scope)
                        else:
                            arg_values.append(parse_value(arg, scope))

                try:
                    return fn(*arg_values, **kwarg_values)
                except OptimizeError:
                    raise
                except Exception as e:
                    raise OptimizeError(f"{fname}(): {e}")

    # Plain attribute access: self.x / obj.x / a.b.c  (no call parens)
    attr_chain = _split_attr_chain(raw)
    if attr_chain:
        root_name, attrs = attr_chain
        if scope.has(root_name):
            value = scope.get(root_name)
            for a in attrs:
                if isinstance(value, OptInstance):
                    value = value.get_attr(a)
                elif isinstance(value, dict):
                    if a not in value:
                        raise OptimizeError(f"'{a}' not found.")
                    value = value[a]
                else:
                    raise OptimizeError(f"Cannot access '.{a}' on non-object value.")
            return value

    try:
        return int(raw)
    except:
        pass

    try:
        return float(raw)
    except:
        pass

    if raw.startswith("["):
        return safe_eval(raw, scope)

    return safe_eval(raw, scope)
# ─────────────────────────────────────────────
#  Assignment: local / global / bare
# ─────────────────────────────────────────────

_UNSET = object()

def assign(name: str, expr: str, scope: Scope, kind: str, precomputed=_UNSET):
    """Assigns `expr` (parsed via parse_value) to `name`, unless
    `precomputed` is given, in which case that value is used as-is
    (used by compound assignment on attribute targets, where the
    right-hand side is already a Python value, not Optimize source)."""
    name = name.strip()

    # Attribute assignment: self.attr = value / obj.attr = value / a.b.c = value
    if "." in name:
        attr_chain = _split_attr_chain(name)
        if not attr_chain:
            raise OptimizeError(f"Invalid assignment target: '{name}'")
        root_name, attrs = attr_chain
        if not scope.has(root_name):
            raise OptimizeError(f"'{root_name}' is not defined.")
        target = scope.get(root_name)
        for a in attrs[:-1]:
            if isinstance(target, OptInstance):
                target = target.get_attr(a)
            elif isinstance(target, dict):
                if a not in target:
                    raise OptimizeError(f"'{a}' not found.")
                target = target[a]
            else:
                raise OptimizeError(f"Cannot access '.{a}' on non-object value.")
        value = precomputed if precomputed is not _UNSET else parse_value(expr.strip(), scope)
        last = attrs[-1]
        if isinstance(target, OptInstance):
            target.set_attr(last, value)
        elif isinstance(target, dict):
            target[last] = value
        else:
            raise OptimizeError(f"Cannot set attribute '.{last}' on non-object value.")
        return value

    if not name.isidentifier():
        raise OptimizeError(f"Invalid variable name: '{name}'")
    value = precomputed if precomputed is not _UNSET else parse_value(expr.strip(), scope)
    if kind == "local":
        scope.set_local(name, value)
    elif kind == "global":
        scope.set_global(name, value)
    else:
        scope.set_auto(name, value)
    return value

def handle_var(rest: str, scope: Scope):
    if "=" not in rest:
        raise OptimizeError(f"Invalid var syntax: 'var {rest}'")
    name, _, expr = rest.partition("=")
    assign(name, expr, scope, "auto")

# ─────────────────────────────────────────────
#  Statements: display / list / add / del / input / type / return
# ─────────────────────────────────────────────

def handle_display(rest: str, scope: Scope):
    rest = rest.strip()
    if not rest:
        raise OptimizeError("display requires a value.")
    value = parse_value(rest, scope)
    print("True" if value is True else "False" if value is False else value)


def handle_open(rest: str, scope: Scope):
    rest = rest.strip()
    if " as " not in rest:
        raise OptimizeError("Invalid open syntax: 'open \"file\" as stdin/stdout'")
    path_part, _, mode_part = rest.partition(" as ")
    path = parse_value(path_part.strip(), scope)
    if not isinstance(path, str):
        raise OptimizeError("File path must be a string.")
    mode = mode_part.strip().lower()
    if mode not in ("stdin", "stdout"):
        raise OptimizeError("open mode must be 'stdin' or 'stdout'.")
    open_file(path, mode)


def handle_read(rest: str, scope: Scope):
    rest = rest.strip()
    if not rest or rest == "-all":
        count = None
    else:
        count = parse_value(rest, scope)
        if isinstance(count, bool):
            count = int(count)
        elif isinstance(count, str):
            if count == "-all":
                count = None
            else:
                count = int(count)
        else:
            count = int(count)
    data = read_lines(count)
    if data:
        write_text(data)
    return data


def handle_write(rest: str, scope: Scope):
    rest = rest.strip()
    if not rest:
        raise OptimizeError("write requires a string value.")
    value = parse_value(rest, scope)
    if not isinstance(value, str):
        value = str(value)
    write_text(value)


def handle_list(rest: str, scope: Scope):
    if "=" not in rest:
        raise OptimizeError(f"Invalid list syntax: 'list {rest}'")
    name, _, expr = rest.partition("=")
    name = name.strip()
    val = safe_eval(expr.strip(), scope)
    if not isinstance(val, list):
        raise OptimizeError("Right-hand side of 'list' must be a list literal.")
    scope.set_auto(name, val)

def handle_add(rest: str, scope: Scope):
    parts = rest.strip().split(None, 1)
    if len(parts) != 2:
        raise OptimizeError(f"Invalid add syntax: 'add {rest}'")
    name, expr = parts
    lst = _get_list(name, scope)
    lst.append(parse_value(expr, scope))

def handle_del(rest: str, scope: Scope):
    rest = rest.strip()
    if "[" in rest and rest.endswith("]"):
        name = rest[:rest.index("[")].strip()
        idx  = int(safe_eval(rest[rest.index("[")+1:-1], scope))
        lst  = _get_list(name, scope)
        if idx < -len(lst) or idx >= len(lst):
            raise OptimizeError(f"Index {idx} out of range.")
        lst.pop(idx)
    else:
        parts = rest.split(None, 1)
        if len(parts) != 2:
            raise OptimizeError(f"Invalid del syntax: 'del {rest}'")
        name, expr = parts
        lst = _get_list(name, scope)
        val = parse_value(expr, scope)
        if val not in lst:
            raise OptimizeError(f"Value {val!r} not found in '{name}'.")
        lst.remove(val)

def _get_list(name: str, scope: Scope):
    if not scope.has(name):
        raise OptimizeError(f"'{name}' is not defined.")
    val = scope.get(name)
    if not isinstance(val, list):
        raise OptimizeError(f"'{name}' is not a list.")
    return val

VALID_INPUT_TYPES = ("int", "float", "str", "bool", "list")

def handle_input(rest: str, scope: Scope):
    rest = rest.strip()
    if not rest:
        raise OptimizeError("input requires a variable name.")

    parts = rest.split(None, 2)

    # Syntax is: input {type_of_var} {var_name} ["prompt"]
    # If the first word isn't a recognized type, there's no type
    # given at all, and the value stays as a plain string.
    if parts[0] in VALID_INPUT_TYPES:
        type_hint = parts[0]
        if len(parts) < 2:
            raise OptimizeError(f"Invalid input syntax: 'input {rest}'")
        var_name = parts[1]
        prompt_part = parts[2] if len(parts) >= 3 else ""
    else:
        type_hint = "str"
        var_name = parts[0]
        prompt_part = parts[1] if len(parts) >= 2 else ""

    prompt = f"{var_name}: "
    if prompt_part:
        p = prompt_part.strip()
        if (p.startswith('"') and p.endswith('"')) or \
           (p.startswith("'") and p.endswith("'")):
            prompt = p[1:-1]

    if not var_name.isidentifier():
        raise OptimizeError(f"Invalid variable name: '{var_name}'")
    try:
        raw = input(prompt).strip()
    except EOFError:
        raw = ""

    # No auto-casting/guessing: the declared type (or plain string
    # if none was given) is the only thing that decides the value.
    if type_hint == "int":
        try:
            value = int(raw)
        except ValueError:
            raise OptimizeError(f"Cannot convert '{raw}' to integer")
    elif type_hint == "float":
        try:
            value = float(raw)
        except ValueError:
            raise OptimizeError(f"Cannot convert '{raw}' to float")
    elif type_hint == "bool":
        if raw.lower() in ("true", "false"):
            value = raw.lower() == "true"
        else:
            raise OptimizeError(f"Cannot convert '{raw}' to boolean (use true or false)")
    elif type_hint == "list":
        try:
            value = safe_eval(raw, scope)
            if not isinstance(value, list):
                raise OptimizeError(f"Cannot convert '{raw}' to list")
        except OptimizeError:
            raise
        except Exception:
            raise OptimizeError(f"Cannot convert '{raw}' to list")
    else:  # "str" - stay as string, no guessing
        value = raw

    scope.set_auto(var_name, value)

def _auto_cast(s: str):
    if s == "True":  return True
    if s == "False": return False
    try:    return int(s)
    except: pass
    try:    return float(s)
    except: pass
    return s

def handle_type(rest: str, scope: Scope):
    val = parse_value(rest.strip(), scope)
    for t, name in [(bool, "Boolean"), (int, "Integer"), (float, "Float"),
                    (list, "List"), (dict, "Dictionary"), (str, "String")]:
        if isinstance(val, t):
            print(name); return
    print("Unknown")

def handle_return(rest: str, scope: Scope):
    if rest.strip():
        raise ReturnException(parse_value(rest.strip(), scope))
    raise ReturnException(None)

# ─────────────────────────────────────────────
#  Library loader
# ─────────────────────────────────────────────

def handle_library(rest: str, scope: Scope):
    name = rest.strip()

    if not name:
        raise OptimizeError("library requires a module name.")

    opt_path = os.path.join(PACKAGE_DIR, f"{name}.opt")
    py_path  = os.path.join(PACKAGE_DIR, f"{name}.py")

    # `library del <name>` — unload a previously loaded library,
    # removing all the names it contributed to global_vars.
    if name.startswith("del "):
        lib_name = name[4:].strip()
        if lib_name not in loaded_libs:
            raise OptimizeError(f"Library '{lib_name}' is not loaded.")
        for sym in lib_exports.get(lib_name, []):
            global_vars.pop(sym, None)
        loaded_libs.discard(lib_name)
        lib_exports.pop(lib_name, None)
        return

    # Load Optimize library
    if os.path.isfile(opt_path):
        with open(opt_path, "r", encoding="utf-8-sig") as f:
            lib_lines = f.readlines()

        execute_block(lib_lines, Scope())
        loaded_libs.add(name)
        return

    # Load Python library
    if os.path.isfile(py_path):
        spec = importlib.util.spec_from_file_location(name, py_path)

        if spec is None or spec.loader is None:
            raise OptimizeError(f"Cannot load Python module '{name}'.")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        exported = []
        for attr in dir(module):
            if attr.startswith("_"):
                continue

            obj = getattr(module, attr)

            if callable(obj):
                global_vars[attr] = obj
                exported.append(attr)
            elif isinstance(obj, (int, float, str, bool, list, tuple)):
                # Export simple constants too (e.g. pi, e) so Python
                # libraries can expose values, not just functions.
                global_vars[attr] = list(obj) if isinstance(obj, tuple) else obj
                exported.append(attr)

        loaded_libs.add(name)
        lib_exports[name] = exported
        return

    raise OptimizeError(
        f"Library '{name}' not found. Expected '{opt_path}' or '{py_path}'."
    )

# ─────────────────────────────────────────────
#  Function call & definition
# ─────────────────────────────────────────────

def call_function(name: str, arg_str: str, scope: Scope):
    if name not in functions:
        raise OptimizeError(f"Function '{name}' is not defined.")
    positional, keywords = _parse_call_args(arg_str, scope)
    fn_scope = _bind_args(name, positional, keywords)
    try:
        execute_block(functions[name]["body"], fn_scope)
    except ReturnException as r:
        return r.value
    return None

def call_function_with_values(name: str, arg_values, kwarg_values=None):
    """Like call_function, but takes already-evaluated Python values
    instead of raw argument strings. Used so Optimize-defined
    'function's can be called from inside a larger expression that
    goes through safe_eval()/Python's eval(), e.g. 'add(1,2) + 3',
    and now also supports keyword arguments, e.g. 'greet(x, y=2)'."""
    if name not in functions:
        raise OptimizeError(f"Function '{name}' is not defined.")
    fn_scope = _bind_args(name, list(arg_values), dict(kwarg_values or {}))
    try:
        execute_block(functions[name]["body"], fn_scope)
    except ReturnException as r:
        return r.value
    return None

# ─────────────────────────────────────────────
#  Single-line dispatcher
# ─────────────────────────────────────────────

def dispatch(line: str, scope: Scope):
    line = line.strip()
    if not line or line.startswith("!"):
        return
    line = _strip_comment(line)
    if not line:
        return

    if line == "escape":        return
    if line == "next":          raise NextException()
    if line == "stop":          raise StopException()

    # `keyword del <name>` — delete a named var/list/library/function
    # entirely, so the user can free something they no longer need.
    # Must be checked BEFORE the normal per-keyword handlers below,
    # since e.g. "var del x" starts with "var " and would otherwise
    # be routed to handle_var and fail as invalid assignment syntax.
    _DEL_PREFIXES = ("var del ", "list del ", "input del ")
    for _pfx in _DEL_PREFIXES:
        if line.startswith(_pfx):
            _name = line[len(_pfx):].strip()
            if not _name.isidentifier():
                raise OptimizeError(f"Invalid name to delete: '{_name}'")
            # For 'function del', also remove from the functions dict.
            if line.startswith("function del ") and _name in functions:
                del functions[_name]
                return
            scope.delete(_name)
            return

    if line.startswith("open "):    return handle_open(line[5:], scope)
    if line == "read" or line.startswith("read "): return handle_read(line[5:].strip(), scope)
    if line.startswith("write "):   return handle_write(line[6:], scope)
    if line.startswith("display "): return handle_display(line[8:], scope)
    if line.startswith("var "):     return handle_var(line[4:], scope)
    if line.startswith("list "):    return handle_list(line[5:], scope)
    if line.startswith("add "):     return handle_add(line[4:], scope)
    if line.startswith("del "):     return handle_del(line[4:], scope)
    if line.startswith("input "):   return handle_input(line[6:], scope)
    if line.startswith("type "):    return handle_type(line[5:], scope)
    if line.startswith("library "): return handle_library(line[8:], scope)
    if line.startswith("return"):   return handle_return(line[6:], scope)

    # Function / method call / instantiation as standalone statement:
    # funcname(...), self.method(...), obj.method(...), ClassName(...)
    call_info = extract_function_call(line)
    if call_info:
        fname, args = call_info

        if fname in classes and not scope.has(fname):
            positional, keywords = _parse_call_args(args, scope)
            instantiate_class(classes[fname], positional, keywords)
            return

        if "." in fname:
            target_expr, _, mname = fname.rpartition(".")
            target = parse_value(target_expr, scope)
            if isinstance(target, OptInstance):
                method = target.cls.find_method(mname)
                if method is None:
                    raise OptimizeError(
                        f"'{target.cls.name}' object has no method '{mname}'.")
                positional, keywords = _parse_call_args(args, scope)
                _call_method(target, method, positional, keywords)
                return
            attr_fn = getattr(target, mname, None) if not isinstance(target, (dict, list)) else None
            if callable(attr_fn):
                positional, keywords = _parse_call_args(args, scope)
                try:
                    attr_fn(*positional, **keywords)
                except OptimizeError:
                    raise
                except Exception as e:
                    raise OptimizeError(f"{fname}(): {e}")
                return
            raise OptimizeError(f"'{fname}' is not callable.")

        if fname in functions:
            call_function(fname, args, scope)
            return
        # Python library function called as a standalone statement
        # (e.g. 'sort(0, len(a))' with no assignment) - same lookup
        # parse_value() already does for expressions, just routed
        # here for bare statements too.
        if scope.has(fname):
            fn = scope.get(fname)
            if callable(fn):
                arg_values = []
                kwarg_values = {}
                if args.strip():
                    for arg in split_top_level(args):
                        arg = arg.strip()
                        m = _KWARG_PATTERN.match(arg)
                        if m:
                            kwarg_values[m.group(1)] = parse_value(m.group(2).strip(), scope)
                        else:
                            arg_values.append(parse_value(arg, scope))
                try:
                    fn(*arg_values, **kwarg_values)
                except OptimizeError:
                    raise
                except Exception as e:
                    raise OptimizeError(f"{fname}(): {e}")
                return

    # Compound assignment: i += 1, i -= 1, self.x += 1, etc.
    for cop in ("+=", "-=", "*=", "/="):
        if cop in line:
            name, _, expr = line.partition(cop)
            name = name.strip()
            if name.isidentifier() and scope.has(name):
                current = scope.get(name)
                delta   = safe_eval(expr.strip(), scope)
                result  = {"+=": current + delta, "-=": current - delta,
                           "*=": current * delta, "/=": current / delta}[cop]
                scope.set_auto(name, result)
                return
            if "." in name and _split_attr_chain(name):
                current = parse_value(name, scope)
                delta   = safe_eval(expr.strip(), scope)
                result  = {"+=": current + delta, "-=": current - delta,
                           "*=": current * delta, "/=": current / delta}[cop]
                assign(name, repr(result) if isinstance(result, str) else str(result), scope, "auto")
                return

    # Bare assignment: x = value, self.attr = value, obj.attr = value
    if "=" in line:
        name, _, expr = line.partition("=")
        name = name.strip()
        if name.isidentifier() or (( "." in name) and _split_attr_chain(name)):
            assign(name, expr, scope, "auto")
            return

    raise OptimizeError(f"Unknown statement: '{line}'")

# ─────────────────────────────────────────────
#  Block collector
# ─────────────────────────────────────────────

def collect_block(lines: list, start: int):
    body, idx, depth = [], start, 0
    while idx < len(lines):
        raw      = lines[idx].rstrip()
        stripped = raw.strip()
        if stripped.startswith("!") or not stripped:
            body.append(raw); idx += 1; continue
        clean = _strip_comment(stripped)

        # ADDED "class " to the tracking tuple here
        if any(clean.startswith(s) for s in ("if (", "for (", "while (", "function ", "class ")):
            depth += 1; body.append(raw); idx += 1
        elif clean == "end":
            if depth == 0:
                break
            depth -= 1; body.append(raw); idx += 1
        elif depth == 0 and any(clean.startswith(s) for s in ("elseif (", "else")):
            break
        else:
            body.append(raw); idx += 1
    return body, idx
def _clean(lines, idx):
    return _strip_comment(lines[idx].strip())

# ─────────────────────────────────────────────
#  Block executors
# ─────────────────────────────────────────────

def execute_block(lines: list, scope: Scope):
    idx = 0
    while idx < len(lines):
        raw      = lines[idx].rstrip()
        stripped = raw.strip()
        if not stripped or stripped.startswith("!"):
            idx += 1; continue
        clean = _strip_comment(stripped)
        if not clean:
            idx += 1; continue

        clean_for_check = clean.rstrip()
        if clean_for_check.endswith(':'):
            clean_for_check = clean_for_check[:-1].rstrip()

        if clean_for_check.startswith("if ("):
            idx = execute_if(lines, idx, scope)
        elif clean_for_check.startswith("for ("):
            idx = execute_for(lines, idx, scope)
        elif clean_for_check.startswith("while ("):
            idx = execute_while(lines, idx, scope)
        elif clean_for_check.startswith("function del "):
            fname = clean_for_check[13:].strip()
            if not fname.isidentifier():
                raise OptimizeError(f"Invalid function name to delete: '{fname}'")
            if fname not in functions:
                raise OptimizeError(f"Cannot delete '{fname}': function is not defined.")
            del functions[fname]
            idx += 1
        elif clean_for_check.startswith("function "):
            idx = register_function(lines, idx)
        # ADDED: Handle class declarations
        elif clean_for_check.startswith("class "):
            idx = register_class(lines, idx)
        elif clean == "end":
            idx += 1
        else:
            dispatch(clean, scope)
            idx += 1
# ── if ───────────────────────────────────────

def execute_if(lines: list, start: int, scope: Scope) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    condition = extract_parens(clean[3:].strip())
    idx += 1
    body, idx = collect_block(lines, idx)

    if safe_eval(condition, scope):
        execute_block(body, scope.child())
        # Skip over any elseif/else branches that follow (they don't
        # run, since the if-condition was already true), consuming
        # the 'end' only once we reach it.
        while idx < len(lines):
            c = strip_trailing_colon(_clean(lines, idx))
            if c.startswith("elseif (") or c == "else":
                idx += 1
                _, idx = collect_block(lines, idx)
            else:
                break
        if idx >= len(lines) or _clean(lines, idx) != "end":
            raise OptimizeError("Expected 'end' after if statement")
        idx += 1
        return idx

    while idx < len(lines):
        c = strip_trailing_colon(_clean(lines, idx))
        if c.startswith("elseif ("):
            ei_cond = extract_parens(c[7:].strip())
            idx += 1
            branch, idx = collect_block(lines, idx)
            if safe_eval(ei_cond, scope):
                execute_block(branch, scope.child())
                while idx < len(lines):
                    cc = strip_trailing_colon(_clean(lines, idx))
                    if cc.startswith("elseif (") or cc == "else":
                        idx += 1
                        _, idx = collect_block(lines, idx)
                    else:
                        break
                if idx >= len(lines) or _clean(lines, idx) != "end":
                    raise OptimizeError("Expected 'end' after if statement")
                idx += 1
                return idx
            continue
        if c == "else":
            idx += 1
            branch, idx = collect_block(lines, idx)
            execute_block(branch, scope.child())
            if idx >= len(lines) or _clean(lines, idx) != "end":
                raise OptimizeError("Expected 'end' after if statement")
            idx += 1
            return idx
        break
    if idx >= len(lines) or _clean(lines, idx) != "end":
        raise OptimizeError("Expected 'end' after if statement")
    idx += 1
    return idx

# ── for ──────────────────────────────────────

def execute_for(lines: list, start: int, scope: Scope) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    inner = extract_parens(clean[3:].strip())
    parts = split_top_level(inner)
    if len(parts) != 3:
        raise OptimizeError(
            f"for loop requires exactly 3 parts (variable, update, condition), got {len(parts)}.")

    var_part, update_stmt, condition = [p.strip() for p in parts]

    idx += 1
    body, idx = collect_block(lines, idx)
    if idx >= len(lines) or _clean(lines, idx) != "end":
        raise OptimizeError("Expected 'end' after for loop body")
    idx += 1

    loop_scope = scope.child()

    if "=" in var_part:
        name, _, expr = var_part.partition("=")
        name = name.strip()
        if not name.isidentifier():
            raise OptimizeError(f"Invalid loop variable: '{var_part}'")
        loop_scope.set_local(name, parse_value(expr.strip(), scope))
    else:
        name = var_part.strip()
        if not name.isidentifier():
            raise OptimizeError(f"Invalid loop variable: '{var_part}'")
        if scope.has(name):
            loop_scope.set_local(name, scope.get(name))
        else:
            raise OptimizeError(
                f"Variable '{name}' is not declared. "
                f"Use 'for ({name} = <start>, ...)' to initialize it inline.")

    def run_update():
        for cop in ("+=", "-=", "*=", "/="):
            if cop in update_stmt:
                uname, _, uexpr = update_stmt.partition(cop)
                uname = uname.strip()
                cur   = loop_scope.get(uname)
                delta = safe_eval(uexpr.strip(), loop_scope)
                new   = {"+=": cur + delta, "-=": cur - delta,
                         "*=": cur * delta, "/=": cur / delta}[cop]
                loop_scope.set_local(uname, new)
                return
        if "=" in update_stmt:
            uname, _, uexpr = update_stmt.partition("=")
            uname = uname.strip()
            loop_scope.set_local(uname, parse_value(uexpr.strip(), loop_scope))
        elif update_stmt:
            dispatch(update_stmt, loop_scope)

    try:
        while safe_eval(condition, loop_scope):
            try:
                execute_block(body, loop_scope)
            except NextException:
                pass
            run_update()
    except StopException:
        pass

    return idx

# ── while ────────────────────────────────────

def execute_while(lines: list, start: int, scope: Scope) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    condition = extract_parens(clean[5:].strip())
    idx += 1
    body, idx = collect_block(lines, idx)
    if idx >= len(lines) or _clean(lines, idx) != "end":
        raise OptimizeError("Expected 'end' after while loop body")
    idx += 1

    try:
        while safe_eval(condition, scope):
            try:
                execute_block(body, scope.child())
            except NextException:
                pass
    except StopException:
        pass

    return idx

# ── function ─────────────────────────────────

def register_function(lines: list, start: int) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    rest  = clean[9:].strip()
    idx  += 1
    if "(" not in rest or not rest.endswith(")"):
        raise OptimizeError(f"Invalid function syntax: 'function {rest}'")
    fname  = rest[:rest.index("(")].strip()
    params, star_args, star_kwargs = parse_params(rest[rest.index("(")+1:-1])
    body, idx = collect_block(lines, idx)
    if idx >= len(lines) or _clean(lines, idx) != "end":
        raise OptimizeError(f"Expected 'end' after function '{fname}'")
    idx += 1
    functions[fname] = {
        "params": params,
        "star_args": star_args,
        "star_kwargs": star_kwargs,
        "body": body,
    }
    return idx

# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────
def register_class(lines: list, start: int) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    rest  = clean[5:].strip()  # Strip 'class'
    idx  += 1

    parent = None
    # Support python-style inheritance: class Child(Parent)
    if "(" in rest:
        if not rest.endswith(")"):
            raise OptimizeError(f"Invalid class syntax: 'class {rest}'")
        cname = rest[:rest.index("(")].strip()
        pname = rest[rest.index("(")+1:-1].strip()
        if pname:
            if pname not in classes:
                raise OptimizeError(f"Parent class '{pname}' is not defined.")
            parent = classes[pname]
    else:
        cname = rest

    if not cname.isidentifier():
        raise OptimizeError(f"Invalid class name: '{cname}'")

    body, idx = collect_block(lines, idx)
    if idx >= len(lines) or _clean(lines, idx) != "end":
        raise OptimizeError(f"Expected 'end' after class '{cname}'")
    idx += 1

    cls = OptClass(cname, parent)

    # Parse methods out of the class block body
    b_idx = 0
    while b_idx < len(body):
        b_raw = body[b_idx].rstrip()
        b_stripped = b_raw.strip()
        if not b_stripped or b_stripped.startswith("!"):
            b_idx += 1
            continue
        b_clean = _strip_comment(b_stripped)
        if not b_clean:
            b_idx += 1
            continue

        b_clean_check = b_clean.rstrip()
        if b_clean_check.endswith(':'):
            b_clean_check = b_clean_check[:-1].rstrip()

        if b_clean_check.startswith("function "):
            f_rest = b_clean_check[9:].strip()
            if "(" not in f_rest or not f_rest.endswith(")"):
                raise OptimizeError(f"Invalid method syntax: 'function {f_rest}'")
            mname = f_rest[:f_rest.index("(")].strip()
            params, star_args, star_kwargs = parse_params(f_rest[f_rest.index("(")+1:-1])
            
            b_idx += 1
            f_body, b_idx = collect_block(body, b_idx)
            if b_idx >= len(body) or _clean(body, b_idx) != "end":
                raise OptimizeError(f"Expected 'end' after method '{mname}'")
            b_idx += 1

            cls.methods[mname] = {
                "name": mname,
                "params": params,
                "star_args": star_args,
                "star_kwargs": star_kwargs,
                "body": f_body,
            }
        elif b_clean == "end":
            b_idx += 1
        else:
            raise OptimizeError(f"Only methods ('function') are allowed directly inside a class body. Got: '{b_clean}'")

    classes[cname] = cls
    return idx

def run(filepath: str):
    try:
        with open(filepath, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Error: File '{filepath}' not found.")
        sys.exit(1)
    try:
        execute_block(lines, Scope())
    except ReturnException:
        pass
    except OptimizeError as e:
        print(f"[Optimize Error] {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python optimize.py <file.opt>")
        sys.exit(1)
    run(sys.argv[1])