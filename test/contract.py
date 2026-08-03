#!/usr/bin/env python3
"""Check the Python client against the Dart schema it talks to.

    python3 test/contract.py

WHY THIS EXISTS, AND WHY THE PYTHON SUITE CANNOT REPLACE IT. Every test in this
repo runs the client against an in-memory fake, and that fake accepts whatever
column it is asked for. A mock that says yes to everything cannot notice a
rename: change `from_identity` to `sender` in lib/src/schemas/messages.dart and
all 205 tests still pass, `./zonai compile` still succeeds because the Dart is
perfectly valid, and the failure appears only at runtime against a real server —
as a 500, or worse as a query that quietly matches nothing.

So the two halves are compared directly. The columns are not hand-listed: they
are RECORDED from what the production code actually put on the wire while the
suite ran, so this cannot drift from real usage the way a manifest would. With
the suite at 100% line coverage, that recording is as complete as the tests are.

WHAT IT DOES NOT CHECK, stated because a guard that overstates its reach is
worse than none: types, nullability, the rules files, and any column reached by
a path no test exercises. It answers one question — does every column this
client names still exist in the schema — and nothing else.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import support  # noqa: E402

# `$.text('created_by', ...)`, `$.id('id', ...)`, `$.createdAt('created_at', ...)`
COLUMN = re.compile(r"\$\.\w+\(\s*['\"]([a-z_][a-z0-9_]*)['\"]")
# `final channels = table('channels', ChannelTable.new);`
TABLE = re.compile(r"\btable\(\s*['\"]([a-z_][a-z0-9_]*)['\"]")


def schema_columns():
    """{table: {column, ...}} as declared in lib/src/schemas/*.dart."""
    schemas = {}
    directory = os.path.join(ROOT, "lib", "src", "schemas")
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".dart"):
            continue
        with open(os.path.join(directory, name)) as f:
            source = f.read()
        tables = TABLE.findall(source)
        if not tables:
            continue
        schemas[tables[0]] = set(COLUMN.findall(source))
    return schemas


def observed_columns():
    """Run the suite; return every column the client actually sent."""
    support.OBSERVED_COLUMNS.clear()
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern="test_*.py",
                            top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit("the suite failed; fix that before trusting this")
    return {t: set(c) for t, c in support.OBSERVED_COLUMNS.items()}


def main():
    declared = schema_columns()
    used = observed_columns()

    if not declared:
        print("no Dart schemas found — nothing to compare", file=sys.stderr)
        return 1

    print("Comparing what the client sends against lib/src/schemas/*.dart\n")
    broken = []
    for table in sorted(used):
        columns = used[table]
        if table not in declared:
            broken.append((table, "TABLE NOT DECLARED", sorted(columns)))
            print("  !! %-14s table is not declared in any schema" % table)
            continue
        missing = sorted(columns - declared[table])
        if missing:
            broken.append((table, "columns missing", missing))
            print("  !! %-14s %d/%d columns — missing: %s"
                  % (table, len(columns) - len(missing), len(columns),
                     ", ".join(missing)))
        else:
            print("  ok %-14s %d columns, all declared" % (table, len(columns)))

    unexercised = {t: sorted(declared[t] - used.get(t, set()))
                   for t in declared if declared[t] - used.get(t, set())}
    if unexercised:
        print("\n  Declared but never sent by the client (not a failure — the"
              "\n  schema may carry more than this client uses):")
        for table, columns in sorted(unexercised.items()):
            print("    %-14s %s" % (table, ", ".join(columns)))

    if broken:
        print("\nCONTRACT BROKEN: the client names %d thing(s) the schema does "
              "not declare." % len(broken), file=sys.stderr)
        print("A rename on the Dart side is invisible to the Python suite, "
              "which runs\nagainst a fake that accepts any column.",
              file=sys.stderr)
        return 1
    print("\nEvery column the client sends is declared in the schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
