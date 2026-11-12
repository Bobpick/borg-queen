import unittest
import server as queen


class FtsTerms(unittest.TestCase):
    def test_drops_stopwords_keeps_ferro_a5_vivaldi(self):
        q = "what is ferro a5, which is used as a substrate for some vivaldi antennas"
        terms = [t.lower() for t in queen.SwarmEngine.content_terms(q)]
        self.assertIn("ferro", terms)
        self.assertIn("a5", terms)
        self.assertIn("vivaldi", terms)
        self.assertIn("antennas", terms)
        self.assertNotIn("what", terms)
        self.assertNotIn("used", terms)
        self.assertNotIn("which", terms)

    def test_vault_search_returns_more_than_five_vivaldi_notes(self):
        hits = queen.swarm.search_with_snippets(
            "what is ferro a5, which is used as a substrate for some vivaldi antennas",
            limit=20,
        )
        self.assertGreaterEqual(len(hits), 8, [h["title"] for h in hits])
        blob = " ".join(h["title"].lower() + " " + h["excerpt"].lower()[:200] for h in hits)
        self.assertTrue("vivaldi" in blob or "ferro" in blob, [h["title"] for h in hits[:8]])

    def test_plan_splits_ferro_query_into_sections(self):
        q = "what is ferro a5, which is used as a substrate for some vivaldi antennas"
        parts = queen.swarm.plan_query_parts(q)
        self.assertGreaterEqual(len(parts), 2)
        blob = " ".join(p["query"].lower() for p in parts)
        self.assertIn("ferro", blob)
        self.assertIn("vivaldi", blob)

    def test_heavy_models_are_70_and_72(self):
        self.assertTrue(queen.SwarmEngine.is_heavy_ram_model("cogito:70b"))
        self.assertTrue(queen.SwarmEngine.is_heavy_ram_model("borg_queen:72b"))
        self.assertFalse(queen.SwarmEngine.is_heavy_ram_model("cogito:32b"))


if __name__ == "__main__":
    unittest.main()
