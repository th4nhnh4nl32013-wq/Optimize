import sys
from main import (
    Scope, dispatch, execute_block, OptimizeError, ReturnException,
    strip_trailing_colon, extract_parens, collect_block, execute_if,
    execute_for, execute_while, register_function, _clean, _strip_comment,
    safe_eval, parse_value
)

def try_eval_expression(clean, scope):
    keywords = ("display ", "var ", "list ", "add ", "del ", "input ",
                "type ", "library ", "return", "if (", "for (", "while (",
                "function ", "next", "stop", "escape")
    if any(clean.startswith(k) or clean == k for k in keywords):
        return False

    for cop in ("+=", "-=", "*=", "/="):
        if cop in clean:
            return False
    if "=" in clean and not any(op in clean for op in ("==", "!=", "<=", ">=")):
        name = clean.split("=", 1)[0].strip()
        if name.isidentifier():
            return False

    try:
        result = safe_eval(clean, scope)
    except OptimizeError:
        return False

    print("True" if result is True else "False" if result is False else result)
    return True

def repl():
    scope = Scope()
    prompt = ">> "
    cont_prompt = ".."

    while True:
        try:
            line = input(prompt)
        except EOFError:
            break

        stripped = line.strip()
        if not stripped:
            continue

        # Exit command
        if stripped.lower() in ("exit", "exit()", "quit", "quit()"):
            break

        clean = strip_trailing_colon(_strip_comment(stripped))

        if clean.startswith("if (") or clean.startswith("for (") or \
           clean.startswith("while (") or clean.startswith("function "):
            block_lines = [line]
            depth = 1
            prompt_level = 1
            while depth > 0:
                sub = input(cont_prompt * (prompt_level + 1))
                sub_clean = strip_trailing_colon(_strip_comment(sub.strip()))
                if sub_clean.startswith(("if (", "for (", "while (", "function ")):
                    depth += 1
                    prompt_level += 1
                elif sub_clean == "end":
                    depth -= 1
                    prompt_level = max(1, prompt_level - 1)
                block_lines.append(sub)

            try:
                execute_block(block_lines, scope)
            except OptimizeError as e:
                print(f"[Optimize Error] {e}")
            except ReturnException:
                pass
            continue

        if try_eval_expression(clean, scope):
            continue

        try:
            dispatch(clean, scope)
        except OptimizeError as e:
            print(f"[Optimize Error] {e}")
        except ReturnException:
            pass

if __name__ == "__main__":
    print("Optimize CMD v0.4")
    print("----------------------------------------")
    repl()
