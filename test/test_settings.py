"""The one tracked file that configures other people's machines.

`.claude/settings.json` is deliberately NOT gitignored: game_loop's hooks live
there written as `"$CLAUDE_PROJECT_DIR"/...`, which is portable and worth
sharing. That makes it the only file in this repo whose contents execute in a
stranger's checkout — so an absolute path in it is not untidy, it is a command
pointing at a directory only this machine has, firing on their every tool call.

That has happened. Reported by another agent who joined from a public repo:
llm_chat's own installer had put an absolute path into their TRACKED
settings.json, and they had to lift it out by hand before pushing. The
installer was fixed; nothing stopped it coming back until this.

The upgraded verify is what surfaced the gap — it reported this path as
matching no rule, which is a better failure than the silence it replaced.
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT  # noqa: E402

SETTINGS = os.path.join(ROOT, ".claude", "settings.json")

# Any absolute path under a user's home. Deliberately broad: the failure is
# "names a directory the reader does not have", and that is true of
# /Users/anyone and /home/anyone alike.
MACHINE_PATH = re.compile(r"(/Users/|/home/)[^/\s\"]+/")


def commands():
    with open(SETTINGS) as f:
        settings = json.load(f)
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                yield event, hook.get("command", "")


class SettingsArePortableTest(unittest.TestCase):
    def test_it_is_valid_json(self):
        """It is merged by two installers. A file that does not parse takes
        every hook in the repo down with it, silently, for whoever clones."""
        with open(SETTINGS) as f:
            self.assertIsInstance(json.load(f), dict)

    def test_NO_HOOK_NAMES_A_PATH_ONLY_THIS_MACHINE_HAS(self):
        for event, command in commands():
            with self.subTest(event=event, command=command[:60]):
                self.assertIsNone(
                    MACHINE_PATH.search(command),
                    "a tracked hook naming an absolute home path fires on "
                    "every tool call in a cloner's checkout, against a "
                    "directory they do not have")

    def test_every_hook_is_anchored_to_the_PROJECT_not_a_checkout(self):
        """`$CLAUDE_PROJECT_DIR` is what makes this file shareable. A command
        that is neither project-anchored nor a bare executable name is one
        that resolved against somebody's working directory."""
        for event, command in commands():
            with self.subTest(event=event, command=command[:60]):
                self.assertTrue(
                    "$CLAUDE_PROJECT_DIR" in command
                    or not command.startswith("/"),
                    "hook command is an absolute path and not anchored to "
                    "$CLAUDE_PROJECT_DIR")

    def test_the_whole_file_is_free_of_machine_paths(self):
        """Not only the hook commands — a statusline, an env var or a future
        key would leak exactly the same way, and this file is edited by
        installers that do not know about this test."""
        with open(SETTINGS) as f:
            found = MACHINE_PATH.findall(f.read())
        self.assertEqual(found, [], "tracked settings.json names %s" % found)

    def test_THE_DETECTOR_ACTUALLY_FIRES(self):
        """#42 from #learnings, applied to this file an hour after writing it.

        Every assertion above says a machine path is ABSENT. None of them
        showed the mechanism that would have produced one actually works — so
        a broken regex makes all of them pass, forever, and passes hardest
        exactly when it is most broken. Proved in both directions here, since
        a detector that matches everything is as useless as one that matches
        nothing."""
        for leak in ("/Users/someone/dev/tool/bin/hook",
                     "/home/someone/dev/tool/bin/hook"):
            with self.subTest(path=leak):
                self.assertIsNotNone(MACHINE_PATH.search(leak))
        for portable in ('"$CLAUDE_PROJECT_DIR"/.game_loop/bin/guard.sh',
                         "python3 test/run.py", "/usr/bin/env python3"):
            with self.subTest(path=portable):
                self.assertIsNone(MACHINE_PATH.search(portable))

    def test_the_check_examined_something(self):
        """"It passed" must not be reachable by finding no hooks at all —
        which is what a renamed key or a restructured file would produce."""
        self.assertTrue(list(commands()),
                        "no hook commands found; the assertions above passed "
                        "without looking at anything")


if __name__ == "__main__":
    unittest.main()
