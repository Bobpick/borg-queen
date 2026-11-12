import tempfile
import unittest
from pathlib import Path

import server as queen


class PromoteInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "wiki"
        self.inbox = self.tmp / "inbox"
        self.vault.mkdir()
        (self.inbox / "papers").mkdir(parents=True)
        self.engine = queen.SwarmEngine(self.vault, self.inbox)

    def test_move_preserves_subfolder_under_wiki_inbox(self):
        src = self.inbox / "papers" / "ferro_a5.md"
        src.write_text("# Ferro A5\nsubstrate for vivaldi\n", encoding="utf-8")
        dest = self.engine.promote_inbox_file(src)
        self.assertEqual(dest, self.vault / "inbox" / "papers" / "ferro_a5.md")
        self.assertTrue(dest.exists())
        self.assertFalse(src.exists())
        self.assertIn("Ferro A5", dest.read_text(encoding="utf-8"))

    def test_index_file_makes_note_searchable(self):
        src = self.inbox / "papers" / "vivaldi.md"
        src.write_text("# Vivaldi antenna on Ferro A5\n", encoding="utf-8")
        dest = self.engine.promote_inbox_file(src)
        self.engine.index_file(dest)
        hits = self.engine.search_with_snippets("vivaldi ferro a5", limit=5)
        titles = [h["title"] for h in hits]
        self.assertIn("vivaldi", titles)

    def test_identical_content_is_not_copied_twice(self):
        first = self.inbox / "papers" / "ferro_a5.md"
        first.write_text("# Ferro A5\nsubstrate\n", encoding="utf-8")
        dest = self.engine.promote_inbox_file(first)
        self.engine.index_file(dest)
        again = self.inbox / "papers" / "ferro_a5_copy.md"
        again.write_text("# Ferro A5\nsubstrate\n", encoding="utf-8")
        skipped = self.engine.promote_inbox_file(again)
        self.assertIsNone(skipped)
        self.assertFalse(again.exists())
        self.assertTrue(dest.exists())

    def test_same_title_different_body_keeps_both(self):
        a = self.inbox / "papers" / "vivaldi.md"
        a.write_text("# paper one\nAAA\n", encoding="utf-8")
        da = self.engine.promote_inbox_file(a)
        self.engine.index_file(da)
        b = self.inbox / "vivaldi.md"
        b.write_text("# paper two\nBBB\n", encoding="utf-8")
        db = self.engine.promote_inbox_file(b)
        self.assertIsNotNone(db)
        self.assertTrue(da.exists())
        self.assertTrue(db.exists())
        self.assertNotEqual(da, db)


if __name__ == "__main__":
    unittest.main()
