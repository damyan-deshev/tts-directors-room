import tempfile
import unittest
from pathlib import Path

from bootstrap_project import build_project_source, manuscript_paragraphs


class BootstrapProjectTests(unittest.TestCase):
    def test_plain_text_and_html_keep_paragraphs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "story.txt"
            plain.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
            html = root / "story.html"
            html.write_text("<h1>Ignored heading</h1><p>First paragraph.</p><p>Second&nbsp; paragraph.</p>", encoding="utf-8")
            expected = ["First paragraph.", "Second paragraph."]
            self.assertEqual(manuscript_paragraphs(plain), expected)
            self.assertEqual(manuscript_paragraphs(html), expected)

    def test_project_source_has_private_provenance_and_neutral_annotations(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "story.md"
            source.write_text("A quiet opening.\n\nA second beat.", encoding="utf-8")
            story, annotations = build_project_source(source, "Demo", "Author", "en", 380)
            self.assertEqual(story["title"], "Demo")
            self.assertEqual(story["beats"][0]["paragraph_ids"], ["p0001", "p0002"])
            self.assertEqual(story["utterances"], [])
            self.assertEqual(annotations["story_sha256"], story["source"]["narration_sha256"])
            self.assertEqual(annotations["global"]["emotion"], "")


if __name__ == "__main__":
    unittest.main()
