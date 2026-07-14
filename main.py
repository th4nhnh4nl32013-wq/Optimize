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
OUTPUT_BUFFER = []
_BL_SUFFIX_PATTERN = _re.compile(r'\s+bl$')
STANDARD_LIB_MODULES = ("optmath", "algorithm", "optrand", "optstr", "opttime")
VALID_INPUT_TYPES = ("int", "float", "bool", "list")
RESERVED_INPUT_TYPE_WORDS = {"str", "string"}
STRING_DECL_KEYWORDS = set()
LIB_UNLOAD_HOOKS = {}

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
    __slots__ = ("cls", "attrs")

    def __init__(self, cls: OptClass):
        self.cls = cls
        self.attrs = {}

    def get_attr(self, name):
        if name in self.attrs:
            return self.attrs[name]
        method = self.cls.find_method(name)
        if method is not None:
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

def flush_buffer():
    if OUTPUT_BUFFER:
        sys.stdout.write("".join(OUTPUT_BUFFER))
        OUTPUT_BUFFER.clear()

def JIT_display(*args):
    # Joins items together and caches them instead of direct heavy printing
    msg = " ".join(str(x) for x in args) + "\n"
    OUTPUT_BUFFER.append(msg)
    if len(OUTPUT_BUFFER) > 20000:  # Periodically flush large chunks
        flush_buffer()

def _call_method(instance: "OptInstance", method: dict, arg_values: list, kwarg_values: dict):
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
    if "(" in text or ")" in text:
        return None
    parts = text.split(".")
    if len(parts) < 2:
        return None
    if not all(p.isidentifier() for p in parts):
        return None
    return parts[0], parts[1:]


def _resolve_start_end_call(target, method_name):
    if method_name == "start" and isinstance(target, (list, tuple, str, dict)):
        return 0
    if method_name == "end" and isinstance(target, (list, tuple, str, dict)):
        return len(target)
    return None

# ─────────────────────────────────────────────
#  Helpers & Caching Engine
# ─────────────────────────────────────────────

SAFE_BUILTINS = {
    "True": True, "False": False, "None": None,
    "len": len, "int": int, "float": float,
    "str": str, "abs": abs, "round": round,
    "min": min, "max": max, "sum": sum,
    "reversed": lambda seq: list(reversed(seq)),
}

_COMPILE_CACHE = {}

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

def _replace_last_item(expr: str) -> str:
    return expr.replace("[last_item]", "[-1]")

_START_END_PATTERN = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(start|end)\(\)")

def _replace_start_end(expr: str) -> str:
    def _sub(m):
        name, which = m.group(1), m.group(2)
        return "0" if which == "start" else f"len({name})"
    return _START_END_PATTERN.sub(_sub, expr)

def safe_eval(expr: str, scope: Scope):
    expr = _replace_last_item(expr)
    expr = _replace_start_end(expr)
    try:
        merged = {**SAFE_BUILTINS, **scope.as_dict()}
        for fname in functions:
            if fname not in merged:
                merged[fname] = (lambda _n: lambda *a, **kw: call_function_with_values(_n, list(a), kw))(fname)
        
        # Bytecode compilation caching bypasses parsing overhead completely
        if expr not in _COMPILE_CACHE:
            _COMPILE_CACHE[expr] = compile(expr, "<string>", "eval")
            
        return eval(_COMPILE_CACHE[expr], {"__builtins__": {}}, merged)
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
    text = text.strip()
    if "(" not in text or not text.endswith(")"):
        return None
    paren_idx = text.index("(")
    name = text[:paren_idx].strip()
    if not name:
        return None
    if not (name.isidentifier() or all(p.isidentifier() for p in name.split("."))):
        return None
    if name in ("if", "for", "while", "function", "class"):
        return None

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
                if i != len(text) - 1:
                    return None
                break
    if depth != 0:
        return None

    args = text[paren_idx+1:-1].strip()
    return (name, args)

# ─────────────────────────────────────────────
#  Pre-Parsed Statement Cache (The Speed Boost)
# ─────────────────────────────────────────────

def pre_parse_block(lines: list) -> list:
    """Pre-parses raw text blocks into opcode tuples so loops run at native speed."""
    parsed_statements = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        clean = _strip_comment(stripped)
        if not clean:
            continue
        
        # Cache standard dispatch tokens
        parsed_statements.append(clean)
    return parsed_statements

def execute_parsed_block(parsed_commands: list, scope: Scope):
    """Executes pre-parsed commands sequentially without text overhead."""
    for command in parsed_commands:
        dispatch(command, scope)

# ─────────────────────────────────────────────
#  Function parameter / call-argument parsing
# ─────────────────────────────────────────────

def parse_params(param_str: str):
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

def _unregister_optstr():
    STRING_DECL_KEYWORDS.discard("string")
    global VALID_INPUT_TYPES
    VALID_INPUT_TYPES = tuple(t for t in VALID_INPUT_TYPES if t not in ("str", "string"))
    RESERVED_INPUT_TYPE_WORDS.add("str")
    RESERVED_INPUT_TYPE_WORDS.add("string")

LIB_UNLOAD_HOOKS["optstr"] = _unregister_optstr

# ─────────────────────────────────────────────
#  Value parser
# ─────────────────────────────────────────────

def _is_pure_string_literal(raw: str):
    if len(raw) < 2:
        return False
    quote = raw[0]
    if quote not in ('"', "'"):
        return False
    if raw[-1] != quote:
        return False
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

    call_info = extract_function_call(raw)

    if call_info:
        fname, args = call_info

        if fname in classes and not scope.has(fname):
            positional, keywords = _parse_call_args(args, scope)
            return instantiate_class(classes[fname], positional, keywords)

        if "." in fname:
            target_expr, _, mname = fname.rpartition(".")
            target = parse_value(target_expr, scope)
            special_value = _resolve_start_end_call(target, mname)
            if special_value is not None:
                return special_value
            if isinstance(target, OptInstance):
                method = target.cls.find_method(mname)
                if method is None:
                    raise OptimizeError(
                        f"'{target.cls.name}' object has no method '{mname}'.")
                positional, keywords = _parse_call_args(args, scope)
                return _call_method(target, method, positional, keywords)
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

        if fname in functions:
            return call_function(fname, args, scope)

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
#  Assignment
# ─────────────────────────────────────────────

_UNSET = object()

def assign(name: str, expr: str, scope: Scope, kind: str, precomputed=_UNSET):
    name = name.strip()

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
#  Statements dispatchers
# ─────────────────────────────────────────────

def handle_display(rest: str, scope: Scope):
    rest = rest.strip()
    if not rest:
        raise OptimizeError("display requires a value.")

    breakline = False
    m = _BL_SUFFIX_PATTERN.search(rest)
    if m:
        breakline = True
        rest = rest[:m.start()].strip()
        if not rest:
            raise OptimizeError("display requires a value.")

    value = parse_value(rest, scope)
    text = "True" if value is True else "False" if value is False else str(value)
    print(text, end="\n" if breakline else "", flush=True)


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

    if parts[0] in RESERVED_INPUT_TYPE_WORDS and parts[0] not in VALID_INPUT_TYPES:
        raise OptimizeError(
            f"input type '{parts[0]}' requires 'library optstr' to be loaded."
        )

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

    prompt = ""
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
    else:
        value = raw

    scope.set_auto(var_name, value)

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

def _load_single_library(name: str):
    opt_path = os.path.join(PACKAGE_DIR, f"{name}.opt")
    py_path  = os.path.join(PACKAGE_DIR, f"{name}.py")

    if os.path.isfile(opt_path):
        with open(opt_path, "r", encoding="utf-8-sig") as f:
            lib_lines = f.readlines()

        execute_block(lib_lines, Scope())
        loaded_libs.add(name)
        return

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
                global_vars[attr] = list(obj) if isinstance(obj, tuple) else obj
                exported.append(attr)

        loaded_libs.add(name)
        lib_exports[name] = exported
        return

    raise OptimizeError(
        f"Library '{name}' not found. Expected '{opt_path}' or '{py_path}'."
    )

def handle_library(rest: str, scope: Scope):
    name = rest.strip()

    if not name:
        raise OptimizeError("library requires a module name.")

    if name.startswith("del "):
        lib_name = name[4:].strip()
        if lib_name == "allstd":
            for mod in STANDARD_LIB_MODULES:
                if mod in loaded_libs:
                    for sym in lib_exports.get(mod, []):
                        global_vars.pop(sym, None)
                    loaded_libs.discard(mod)
                    lib_exports.pop(mod, None)
            return
        if lib_name not in loaded_libs:
            raise OptimizeError(f"Library '{lib_name}' is not loaded.")
        for sym in lib_exports.get(lib_name, []):
            global_vars.pop(sym, None)
        loaded_libs.discard(lib_name)
        lib_exports.pop(lib_name, None)
        return

    if name == "allstd":
        missing = []
        for mod in STANDARD_LIB_MODULES:
            if mod in loaded_libs:
                continue
            try:
                _load_single_library(mod)
            except OptimizeError:
                missing.append(mod)
        if missing:
            raise OptimizeError(
                f"library allstd: could not find module(s): {', '.join(missing)}"
            )
        return

    _load_single_library(name)


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
    # We no longer strip comments here since it's pre-stripped during collection/parsing steps
    if not line:
        return

    if line == "escape":        return
    if line == "next":          raise NextException()
    if line == "stop":          raise StopException()

    _DEL_PREFIXES = ("var del ", "list del ", "input del ")
    for _pfx in _DEL_PREFIXES:
        if line.startswith(_pfx):
            _name = line[len(_pfx):].strip()
            if not _name.isidentifier():
                raise OptimizeError(f"Invalid name to delete: '{_name}'")
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
    if line.startswith("string "):
        if "string" not in STRING_DECL_KEYWORDS:
            raise OptimizeError("'string' declarations require 'library optstr' to be loaded.")
        rest = line[7:].strip()
        if "=" not in rest:
            raise OptimizeError(f"Invalid string syntax: 'string {rest}'")
        name, _, expr = rest.partition("=")
        name = name.strip()
        if not name.isidentifier():
            raise OptimizeError(f"Invalid variable name: '{name}'")
        value = parse_value(expr.strip(), scope)
        if not isinstance(value, str):
            raise OptimizeError(f"'string' declaration requires a string value, got {type(value).__name__}.")
        assign(name, expr, scope, "auto", precomputed=value)
        return

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
            special_value = _resolve_start_end_call(target, mname)
            if special_value is not None:
                return
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

    for cop in ("+=", "-=", "*=", "/="):
        if cop in line:
            name, _, expr = line.partition(cop)
            name = name.strip()
            if name.isidentifier() and scope.has(name):
                current = scope.get(name)
                delta   = safe_eval(expr.strip(), scope)
                result  = {
                    "+=": current + delta,
                    "-=": current - delta,
                    "*=": current * delta,
                    "/=": current / delta,
                    "//=": current // delta,
                }[cop]
                scope.set_auto(name, result)
                return
            if "." in name and _split_attr_chain(name):
                current = parse_value(name, scope)
                delta   = safe_eval(expr.strip(), scope)
                result  = {
                    "+=": current + delta,
                    "-=": current - delta,
                    "*=": current * delta,
                    "/=": current / delta,
                    "//=": current // delta,
                }[cop]
                assign(name, repr(result) if isinstance(result, str) else str(result), scope, "auto")
                return

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
    
    var_part, condition, update_stmt = [p.strip() for p in parts]

    idx += 1
    body, idx = collect_block(lines, idx)
    idx += 1  # Skip 'end'

    # Handle loop initialization (e.g., i=1)
    if "=" in var_part:
        var_name, _, init_expr = var_part.partition("=")
        var_name = var_name.strip()
        scope.set_auto(var_name, parse_value(init_expr.strip(), scope))

    # Translate body to Python statements
    py_body_lines = []
    parsed_body = pre_parse_block(body)
    
    for cmd in parsed_body:
        cmd_str = cmd.strip()
        
        # Strip internal 'var ' declarations inside the loop to avoid local scope re-allocation overhead
        if cmd_str.startswith("var "):
            cmd_str = cmd_str[4:].strip()

        # Translate display statement to use our fast buffered system
        if cmd_str.startswith("display "):
            expr = cmd_str[8:].strip()
            # Convert string concatenation '+' to commas for python print arguments if needed, 
            # or keep it as a clean expression evaluation
            py_body_lines.append(f"    JIT_display({expr})")
            continue

        # Handle typical assignments/mutations
        translated = False
        for cop in ("+=", "-=", "*=", "/="):
            if cop in cmd_str:
                target, _, expr = cmd_str.partition(cop)
                py_body_lines.append(f"    {target.strip()} {cop} {expr.strip()}")
                translated = True
                break
        
        if not translated and "=" in cmd_str:
            target, _, expr = cmd_str.partition("=")
            py_body_lines.append(f"    {target.strip()} = {expr.strip()}")
            translated = True
            
        if not translated:
            py_body_lines.append(f"    dispatch({repr(cmd_str)}, scope)")

    # Translate standard unary increment/decrement patterns like i++ or i--
    if update_stmt.endswith("++"):
        v_name = update_stmt[:-2].strip()
        py_body_lines.append(f"    {v_name} += 1")
    elif update_stmt.endswith("--"):
        v_name = update_stmt[:-2].strip()
        py_body_lines.append(f"    {v_name} -= 1")
    elif "+=" in update_stmt or "=" in update_stmt:
        py_body_lines.append(f"    {update_stmt}")

    # Build loop compilation structure
    # Convert operators like '<=' to native python syntax if needed
    py_condition = condition.replace("<=", "<=") 
    
    loop_code = f"while {py_condition}:\n" + "\n".join(py_body_lines)

    # Prepare execution environment
    context = {
        **SAFE_BUILTINS, 
        **scope.as_dict(), 
        "dispatch": dispatch, 
        "scope": scope, 
        "JIT_display": JIT_display
    }
    
    # Run loop code at machine speed
    exec(loop_code, {"__builtins__": {}}, context)

    # Flush any remaining items in the print buffer
    flush_buffer()

    # Synchronize values back to interpreter state
    for k, v in context.items():
        if scope.has(k) or k in (var_name, 'ref'):
            scope.set_auto(k, v)

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

    py_body_lines = []
    parsed_body = pre_parse_block(body)
    
    for cmd in parsed_body:
        translated = False
        for cop in ("+=", "-=", "*=", "/="):
            if cop in cmd:
                target, _, expr = cmd.partition(cop)
                target = target.strip()
                if target.isidentifier():
                    py_body_lines.append(f"    {target} {cop} {expr}")
                    translated = True
                    break
        if not translated and "=" in cmd:
            target, _, expr = cmd.partition("=")
            target = target.strip()
            if target.isidentifier():
                py_body_lines.append(f"    {target} = {expr}")
                translated = True
                
        if not translated:
            py_body_lines.append(f"    dispatch({repr(cmd)}, scope)")

    loop_code = f"while {condition}:\n" + "\n".join(py_body_lines)

    context = {**SAFE_BUILTINS, **scope.as_dict(), "dispatch": dispatch, "scope": scope}
    exec(loop_code, {"__builtins__": {}}, context)

    for k, v in context.items():
        if scope.has(k):
            scope.set_auto(k, v)

    return idx
# ── function / class mechanics ───────────────

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

def register_class(lines: list, start: int) -> int:
    idx   = start
    clean = strip_trailing_colon(_clean(lines, idx))
    rest  = clean[5:].strip()
    idx  += 1

    parent = None
    if "(" in rest:
        if not rest.endswith(")"):
            raise OptimizeError(f"Invalid class syntax: 'class {rest}'")
        cname = rest[:rest.index("(")].strip()
        pname = rest[rest.index("(")+1:-1].strip()
        if pname:
            if pname not in classes:
                raise
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
            raise OptimizeError(f"Only methods are allowed inside a class body. Got: '{b_clean}'")

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