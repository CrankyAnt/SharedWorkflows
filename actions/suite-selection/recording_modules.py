"""The modules of the shared CI that the recording step loads when it runs.

They are ``recording.py`` and the modules it reaches through imports: the module-level imports of each module it
loads, and the imports inside the functions of ``recording.py`` itself. An import inside a function of another
module counts only when the recording step calls that function, which a test of the recording step's commands
guards; ``suite_selection.py`` imports ``reuse`` and ``unrecorded`` only inside the functions of ``select``, the
lookup and the store. These modules are the files of the shared digest that ``recording.py digest`` takes, and
a change to one of them runs every entry file of the jobs that use the recording step, as "Unrecorded inputs" in
``docs/maintaining.md`` states.

Paths are strings with ``/`` separators, so that the same function reads a checkout's files and a revision's.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence

TYPE_CHECKING_CONDITIONS = ("TYPE_CHECKING", "typing.TYPE_CHECKING")


def imported_names(text: str, *, inside_functions: bool) -> set[str]:
    """The top-level names that a module imports: those of its module-level imports, inside ``if``, ``try`` and
    ``with`` blocks included and outside ``if TYPE_CHECKING:``, and with ``inside_functions`` every import, those inside
    its functions and classes included."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    if inside_functions:
        nodes = list(ast.walk(tree))
    else:
        nodes, pending = [], list(tree.body)
        while pending:
            node = pending.pop()
            nodes.append(node)
            if isinstance(node, (ast.If, ast.Try, ast.With)):
                # The body of ``if TYPE_CHECKING:`` runs only under a type checker.
                blocks = ("orelse",) if isinstance(node, ast.If) and ast.unparse(node.test) in TYPE_CHECKING_CONDITIONS else ("body", "orelse", "finalbody", "handlers")
                for block in blocks:
                    for child in getattr(node, block, ()):
                        pending.extend(child.body if isinstance(child, ast.ExceptHandler) else [child])
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def loaded_modules(script: str, read: Callable[[str], str | None], search: Sequence[str]) -> list[str]:
    """``script`` and the modules it loads when it runs, in the order found. A name resolves to the first
    ``<folder>/<name>.py`` of the folders of ``search`` that ``read`` finds; ``read`` gives a file's text, or None."""
    found: list[str] = []
    pending = [script]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.append(path)
        for name in sorted(imported_names(read(path) or "", inside_functions=path == script)):
            module = next((candidate for candidate in (f"{folder}/{name}.py" for folder in search) if read(candidate) is not None), None)
            if module is not None:
                pending.append(module)
    return found
