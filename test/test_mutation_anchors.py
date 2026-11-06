"""Every mutation still has code to mutate.

A mutation re-introduces a shipped bug by replacing an exact snippet of the
source. When the code around that snippet is rewritten, the snippet is gone,
the mutation applies to nothing, and the nightly sweep reports it as a
survivor ("anchor no longer present — mutation stale") days after the commit
that caused it. Issue #42 was two of those, both from this repo's own
changes (the doorbell name in #53, the inbox wake in #51).

Checked here, at commit time, where the change that broke it is still on
screen.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate  # noqa: E402


class MutationAnchorTest(unittest.TestCase):
    def test_every_mutation_anchor_is_in_its_file(self):
        missing = []
        for mutation in mutate.MUTATIONS:
            name, path, original = mutation[0], mutation[1], mutation[2]
            with open(os.path.join(mutate.ROOT, path)) as f:
                if original not in f.read():
                    missing.append("%s (%s)" % (name, path))
        self.assertEqual(missing, [],
                         "these mutations no longer match their code; update "
                         "the snippet so it re-introduces the same bug")

    def test_every_mutation_anchor_is_in_its_file_ONCE(self):
        """Present is not enough. #60 copied a line a mutation anchored on
        into a second function; the anchor still matched, so the test above
        passed, and the sweep refused it as AMBIGUOUS every night for two
        weeks (#44)."""
        ambiguous = []
        for mutation in mutate.MUTATIONS:
            name, path, original = mutation[0], mutation[1], mutation[2]
            with open(os.path.join(mutate.ROOT, path)) as f:
                count = f.read().count(original)
            if count > 1:
                ambiguous.append("%s (%s, %d matches)" % (name, path, count))
        self.assertEqual(ambiguous, [],
                         "these anchors match more than one place, so the "
                         "sweep refuses them; lengthen the snippet until it "
                         "names one")


if __name__ == "__main__":
    unittest.main()
