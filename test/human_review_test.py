"""Human review HTML is a local survey with safe JSON export."""

import unittest

from alpha.human_review import render_human_review
from alpha.qualitative import human_review_template


class HumanReviewPageTests(unittest.TestCase):
    def test_ratings_pairs_and_blinded_display(self):
        report = {
            "id": "e1", "selected": "candidate",
            "variants": [{"id": "baseline"}, {"id": "candidate"}],
            "runs": [
                {
                    "split": "holdout", "case_id": "task", "trial": 0,
                    "variant": variant, "final_state": {"artifact": content},
                    "trace": [{"context": {"request": "Write a summary"}}],
                } for variant, content in (
                    ("baseline", "Original reply"), ("candidate", "Changed reply")
                )
            ],
        }
        template = human_review_template(report)
        self.assertEqual(len(template["scores"]), 2)
        self.assertEqual(len(template["pairs"]), 1)
        html = render_human_review(report, template)
        self.assertIn("1–5 stars", html)
        self.assertIn("A/B", html)
        self.assertIn("Original reply", html)
        self.assertIn("Changed reply", html)
        self.assertIn("human-review-completed.json", html)
        self.assertIn("a_is_baseline", html)
        self.assertIn("data-star-notes", html)
        self.assertNotIn('<h3>baseline</h3>', html)
        self.assertNotIn('<h3>candidate</h3>', html)

    def test_untrusted_outputs_cannot_break_json_script(self):
        report = {
            "id": "e1", "selected": "candidate",
            "variants": [{"id": "baseline"}, {"id": "candidate"}],
            "runs": [
                {
                    "split": "holdout", "case_id": "task", "trial": 0,
                    "variant": variant,
                    "final_state": {"artifact": '</script><script>alert("x")</script>'},
                    "trace": [],
                } for variant in ("baseline", "candidate")
            ],
        }
        html = render_human_review(report, human_review_template(report))
        self.assertNotIn('</script><script>alert("x")', html)
        self.assertIn(r'\u003c/script\u003e', html)


if __name__ == "__main__":
    unittest.main()
