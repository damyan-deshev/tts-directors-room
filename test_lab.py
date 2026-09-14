import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class LaboratoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        first = "A quiet opening."
        second = "“Again,” said Mara."
        narration_hash = hashlib.sha256(f"{first}\n\n{second}".encode()).hexdigest()
        cls.story = {
            "version": 2,
            "title": "The Lighthouse",
            "author": "A. Writer",
            "language": "en",
            "source": {"narration_sha256": narration_hash},
            "paragraphs": [
                {"id": "p001", "text": first, "chars": len(first)},
                {"id": "p002", "text": second, "chars": len(second)},
            ],
            "beats": [{"id": "b001", "title": "Opening", "paragraph_ids": ["p001", "p002"]}],
            "utterances": [{
                "id": "u001",
                "paragraph_id": "p002",
                "beat_id": "b001",
                "speaker": "Mara",
                "speaker_kind": "character",
                "start": 0,
                "end": len("“Again,”"),
                "text": "“Again,”",
                "preview_paragraph_ids": ["p001", "p002"],
                "suggested_emotion": "amusement",
            }],
        }
        cls.annotations = {
            "version": 3,
            "story_sha256": narration_hash,
            "include_title": True,
            "target_chars": 380,
            "global": {"emotion": "", "expressiveness": "expressive_low"},
            "utterances": {"u001": {"emotion": "amusement"}},
        }
        cls.lab = server.default_lab_state(cls.story, cls.annotations)
        cls.lab["voice_mode"] = "canonical_narrator"

    def test_migration_keeps_title_author_and_paragraphs(self):
        self.assertIn("The Lighthouse", self.lab["text"])
        self.assertIn("By A. Writer", self.lab["text"])
        self.assertIn("A quiet opening.", self.lab["text"])

    def test_migration_makes_old_overrides_visible(self):
        self.assertIn("<|emotion:amusement|>", self.lab["text"])
        self.assertIn("“Again,”", self.lab["text"])

    def test_chunk_offsets_cover_the_exact_editable_text(self):
        chunks = server.plan_lab_chunks(self.lab["text"], self.lab["target_chars"])
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[-1]["end"], len(self.lab["text"]))
        rebuilt = "".join(
            self.lab["text"][chunk["start"]:chunk["end"]] for chunk in chunks
        )
        self.assertEqual(rebuilt, self.lab["text"])

    def test_higgs_tags_do_not_count_as_spoken_characters(self):
        source = "<|emotion:amusement|>Кратък тест."
        chunk = server.plan_lab_chunks(source, 380)[0]
        self.assertEqual(chunk["characters"], len("Кратък тест."))
        self.assertEqual(chunk["tags"], ["<|emotion:amusement|>"])

    def test_malformed_or_unsupported_inline_higgs_tags_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Malformed or unsupported"):
            server.validate_inline_higgs_tags("Текст <|emotion:sadness")
        with self.assertRaisesRegex(ValueError, "Unsupported inline"):
            server.validate_inline_higgs_tags("<|emotion:invented|>Текст")
        server.validate_inline_higgs_tags(
            "<|emotion:sadness|><|style:whispering|><|prosody:expressive_low|>Текст"
        )

    def test_preview_request_is_one_preplanned_higgs_chunk(self):
        chunk = server.plan_lab_chunks(self.lab["text"], self.lab["target_chars"])[0]
        prompt, request = server.lab_generation_request(self.lab, chunk)
        self.assertNotIn("\n", prompt)
        self.assertEqual(request["chunk_chars"], 5000)
        self.assertFalse(request["rolling_context"])
        self.assertEqual(request["voice_mode"], "canonical_narrator")
        self.assertEqual(request["controls"], self.lab["controls"])

    def test_default_delivery_uses_the_validated_speaker_safe_baseline(self):
        self.assertEqual(self.lab["controls"]["emotion"], "")
        self.assertEqual(self.lab["controls"]["expressiveness"], "expressive_low")
        diagnostics = server.delivery_diagnostics("Кратък тест.", self.lab["controls"])
        self.assertEqual(diagnostics["global_token_count"], 1)
        self.assertFalse(diagnostics["stacked_global_controls"])

    def test_stacked_delivery_controls_are_reported(self):
        diagnostics = server.delivery_diagnostics(
            "<|emotion:amusement|>Кратък тест.",
            {"emotion": "sadness", "expressiveness": "expressive_high"},
        )
        self.assertEqual(diagnostics["global_token_count"], 2)
        self.assertEqual(diagnostics["inline_token_count"], 1)
        self.assertTrue(diagnostics["stacked_global_controls"])

    def test_backend_must_confirm_the_requested_stable_voice(self):
        confirmation = server.confirmed_backend_voice(
            {
                "voice_mode": "canonical_narrator",
                "stable_anchor": True,
                "anchor_name": "Canonical narrator",
                "voice_cloned": True,
            },
            "canonical_narrator",
        )
        self.assertEqual(confirmation["voice_mode"], "canonical_narrator")
        self.assertEqual(
            confirmation["reference_fingerprint_attestation"],
            "unavailable_from_endpoint",
        )
        verified = server.confirmed_backend_voice(
            {
                "voice_mode": "canonical_narrator",
                "stable_anchor": True,
                "anchor_name": "Canonical narrator",
                "voice_cloned": True,
                "reference_audio_sha256": "a",
                "reference_text_sha256": "t",
            },
            "canonical_narrator",
            {"audio_sha256": "a", "transcript_sha256": "t"},
        )
        self.assertEqual(verified["reference_fingerprint_attestation"], "verified")
        with self.assertRaises(RuntimeError):
            server.confirmed_backend_voice(
                {
                    "voice_mode": "canonical_narrator",
                    "stable_anchor": True,
                    "voice_cloned": True,
                    "reference_audio_sha256": "wrong",
                    "reference_text_sha256": "t",
                },
                "canonical_narrator",
                {"audio_sha256": "a", "transcript_sha256": "t"},
            )
        with self.assertRaises(RuntimeError):
            server.confirmed_backend_voice(
                {
                    "voice_mode": "natural",
                    "stable_anchor": False,
                    "voice_cloned": False,
                },
                "canonical_narrator",
            )

    def test_voice_inventory_exposes_no_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "reference.wav"
            transcript = root / "reference.txt"
            audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            transcript.write_text("Reference transcript.", encoding="utf-8")
            registry = {
                "canonical_narrator": {
                    "label": "Canonical narrator",
                    "description": "Test reference",
                    "audio": audio,
                    "transcript": transcript,
                }
            }
            with mock.patch.object(server, "VOICE_REFERENCES", registry):
                voices = server.available_voices()
            self.assertEqual(len(voices), 1)
            self.assertTrue(all("audio" not in voice for voice in voices))
            self.assertTrue(all("transcript" not in voice for voice in voices))
            self.assertTrue(all(voice["preview_url"].startswith("/reference-audio/") for voice in voices))

    def test_asr_metrics_detect_internal_omission_and_repetition(self):
        expected = "едно две три четири пет шест седем осем девет десет единайсет дванайсет"
        self.assertTrue(server.transcript_metrics(expected, expected)["content_acceptable"])
        omitted = "едно две три осем девет десет единайсет дванайсет"
        self.assertFalse(server.transcript_metrics(expected, omitted)["content_acceptable"])
        repeated = expected + " пет шест седем осем пет шест седем осем"
        self.assertFalse(server.transcript_metrics(expected, repeated)["content_acceptable"])

    def test_spoken_higgs_text_strips_direction_tokens(self):
        value = server.spoken_higgs_text("<|emotion:sadness|> Текст <|style:whispering|> тук.")
        self.assertEqual(value, "Текст тук.")


if __name__ == "__main__":
    unittest.main()
