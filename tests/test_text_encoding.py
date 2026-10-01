"""Text file IO is UTF-8 everywhere, never the locale encoding (cp1252 on Windows).

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
TEMPLATES_ADWS = SKILL / "templates" / "adws"
CHECKED = [*TEMPLATES_ADWS.rglob("*.py"), *(SKILL / "scripts").glob("*.py")]

sys.path.insert(0, str(TEMPLATES_ADWS))
from adw_modules import prompts  # noqa: E402

TEXT_METHODS = {"read_text", "write_text", "open"}


def _binary_mode(call: ast.Call) -> bool:
    """open(p, "rb") / p.open("ab") / mode="wb": bytes, so no encoding applies."""
    first = 1 if isinstance(call.func, ast.Name) else 0  # open(path, mode) vs path.open(mode)
    mode = call.args[first] if len(call.args) > first else None
    mode = next((k.value for k in call.keywords if k.arg == "mode"), mode)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def _decodes_text(call: ast.Call) -> bool:
    """subprocess.run(..., text=True) and friends decode the child's output."""
    return any(k.arg in ("text", "universal_newlines") and isinstance(k.value, ast.Constant)
               and k.value.value is True for k in call.keywords)


def unencoded_calls(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else None)
        if not (name in TEXT_METHODS and not _binary_mode(node)) and not _decodes_text(node):
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        found.append(f"{path.relative_to(SKILL)}:{node.lineno}: {name}() without encoding=")
    return found


class TextEncodingTest(unittest.TestCase):
    def test_every_text_read_write_open_and_subprocess_names_its_encoding(self):
        self.assertTrue(CHECKED)
        found = [line for path in CHECKED for line in unencoded_calls(path)]
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_a_prompt_with_non_cp1252_text_is_saved_and_rendered_as_utf8(self):
        text = "x − y → z “quoted”"  # U+2212 and friends: not in cp1252
        with tempfile.TemporaryDirectory() as tmp:
            saved = prompts.save(tmp, "user.md", text)
            self.assertEqual(saved.read_bytes(), text.encode("utf-8"))
            self.assertEqual(prompts.render(saved, {}), text)


if __name__ == "__main__":
    unittest.main()
