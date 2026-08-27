"""How often does prose in this repo state the value of a named constant?

RUN BEFORE BUILDING A CHECK FOR IT, and the answer was: do not build one.

    prose mentions of a constant WITH a number nearby   1
      the number matches the constant                   1
      it does not                                       0

THE ARGUMENT THIS SETTLED. A day of stale-prose defects — "reverts eleven
fixes" (230), "default 200" (600), "the suite fingerprints .llm_chat/ and
.claude/" (it asks git) — plus wcs finding a module docstring that described
the pre-fix world two hours after the fix landed, in the same file, written by
them. From that I said a blind-spot note is a stale exemption in prose and
that I had no mechanism for it.

Measured, the mechanism I was reaching for has ONE instance in this repo and
it agrees. A scan would be a gate with no population — which is the same
mistake as the runaway detector deleted earlier the same day, and it would
have been made immediately after deleting that one for exactly this reason.

WHY THE POPULATION IS EMPTY, since the defects were real. They were not
constants-in-source-prose. They were counts of GROWING SETS (a list's length,
a test count) and descriptions of BEHAVIOUR, and they lived in README and
llms.txt rather than in docstrings. The two that recurred are checked where
they actually live: `test_wiring.CapNumberTest` binds any cap number in the
documents to the code, and `test_no_start_command_ANYWHERE_omits_the_bind`
scans them for a server start command with no bind address.

What caught the rest was not a scanner. It was the tests — a change to
DEFAULT_MAX_MESSAGES that broke nothing revealed that nothing referenced it —
and wcs's own move: taking a claim to the artifact. The transferable thing is
a test that makes the author name their evidence, which is what kept "NO
RECORD" from being written as "STOPPED". That is a practice, not a scan, and
this file exists to record that the scan was measured and refused.

Reports agreement and disagreement separately. A scan that only prints
mismatches cannot tell "nothing is wrong" from "nothing was examined".

    python3 test/audit_prose_constants.py
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["bin/llm_chat", "bin/llm-chat-deliver", "bin/llm-chat-wake",
         "bin/llm-chat-slack", "bin/llm-chat-mcp",
         "test/run.py", "test/mutate.py"]


def constants(tree):
    """Module-level NAME = <int>."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
            if (isinstance(target, ast.Name) and target.id.isupper()
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, int)
                    and not isinstance(value.value, bool)):
                found[target.id] = value.value
    return found


def prose(path, source):
    """Every comment and docstring, as (line, text)."""
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            yield number, stripped
    tree = ast.parse(source)
    for node in ast.walk(tree):
        doc = ast.get_docstring(node) if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                   ast.AsyncFunctionDef)) else None
        if doc:
            yield getattr(node, "lineno", 0), doc


agree = disagree = mentions = 0
rows = []
for name in FILES:
    path = os.path.join(ROOT, name)
    try:
        with open(path) as handle:
            source = handle.read()
    except OSError:
        continue
    known = constants(ast.parse(source))
    if not known:
        continue
    for line, text in prose(path, source):
        for const, value in known.items():
            for hit in re.finditer(re.escape(const), text):
                window = text[hit.end():hit.end() + 60]
                numbers = [int(n) for n in re.findall(r"\b(\d{1,7})\b", window)]
                if not numbers:
                    continue
                mentions += 1
                if value in numbers:
                    agree += 1
                else:
                    disagree += 1
                    rows.append("%s:%s  %s says %s, code says %d"
                                % (name, line, const, numbers[:3], value))

print("prose mentions of a constant WITH a number nearby   %d" % mentions)
print("  the number matches the constant                   %d" % agree)
print("  it does not                                       %d" % disagree)
for row in rows:
    print("    %s" % row)
if not mentions:
    print("\nNO POPULATION. A check for this would be a gate over nothing.")
    sys.exit(4)
