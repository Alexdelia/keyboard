import re

TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n"}
UNESCAPES = {"n": "\n"}


class Sym(str):
    pass


def parse(text: str) -> list:
    stack: list[list] = [[]]
    position = 0
    end = len(text.rstrip())
    while position < end:
        match = TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"unexpected input at {position}")
        position = match.end()
        opening, closing, quoted, bare = match.groups()
        if opening:
            stack.append([])
        elif closing:
            finished = stack.pop()
            stack[-1].append(finished)
        elif quoted is not None:
            stack[-1].append(re.sub(r"\\(.)", _unescape, quoted))
        else:
            stack[-1].append(Sym(bare))
    (tree,) = stack[0]
    return tree


def dump(tree: list) -> str:
    return "".join(_lines(tree, 0)) + "\n"


def _unescape(match: re.Match) -> str:
    return UNESCAPES.get(match.group(1), match.group(1))


def _atom(value) -> str:
    if isinstance(value, Sym):
        return value
    if isinstance(value, str):
        return '"' + "".join(ESCAPES.get(c, c) for c in value) + '"'
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return _number(value)
    return str(value)


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _lines(tree: list, depth: int):
    indent = "\t" * depth
    split = next(
        (i for i, item in enumerate(tree) if isinstance(item, list)), len(tree)
    )
    atoms, children = tree[:split], tree[split:]
    if any(not isinstance(child, list) for child in children):
        raise ValueError(f"atom after list in {tree[0]}")
    head = " ".join(_atom(item) for item in atoms)
    if not children:
        yield f"{indent}({head})"
        return
    if all(child and child[0] == "xy" for child in children):
        points = " ".join(
            f"({' '.join(_atom(a) for a in child)})" for child in children
        )
        yield f"{indent}({head}\n{indent}\t{points}\n{indent})"
        return
    yield f"{indent}({head}\n"
    for child in children:
        yield from _lines(child, depth + 1)
        yield "\n"
    yield f"{indent})"
