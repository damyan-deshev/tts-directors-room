from copy import deepcopy
import json
from array import array
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import directors_room as director
import server
from project_store import ProjectStore


def sample_project():
    story = {
        "title": "Fixture",
        "author": "Test",
        "source": {"narration_sha256": "story-hash"},
    }
    text = "Първи регион.\n\nВтори регион.\n\nТрети регион."
    lab = {
        "text": text,
        "voice_mode": "canon",
        "controls": {"emotion": "", "style": "", "speed": "", "pitch": "", "expressiveness": "expressive_low"},
        "temperature": 0.8,
        "top_k": 50,
        "seed": 42,
        "max_tokens": 3000,
    }
    chunks = []
    cursor = 0
    for index, value in enumerate(text.split("\n\n"), 1):
        start = text.index(value, cursor)
        end = start + len(value)
        if index < 3:
            end += 2
        chunks.append({
            "id": f"c{index:03d}", "index": index, "start": start, "end": end,
            "boundary_after": "paragraph",
        })
        cursor = end
    return director.new_project(story, lab, chunks), lab


def voice():
    return {
        "id": "canon", "label": "Canonical anchor",
        "audio_sha256": "audio-anchor", "transcript_sha256": "text-anchor",
    }


def endpoint():
    return {"model": "bosonai/higgs-tts-3-4b", "backend": "MLX", "build": "fixture"}


class DirectorsRoomTests(unittest.TestCase):
    def test_only_canonical_reference_is_conditioning(self):
        project, _ = sample_project()
        transaction = director.current_transaction(project, project["regions"][1], voice(), "http://higgs", endpoint())
        self.assertEqual(transaction["conditioning"]["source"], "immutable_canonical_reference_only")
        self.assertFalse(transaction["conditioning"]["neighbor_takes_applied"])
        self.assertFalse(transaction["continuity_context"]["applied_to_higgs"])
        self.assertFalse(transaction["higgs_request"]["rolling_context"])

    def test_upstream_take_change_does_not_change_generation_freshness(self):
        project, _ = sample_project()
        first, second = project["regions"][:2]
        first["takes"] = [
            {"id": "take_001", "audio_sha256": "one"},
            {"id": "take_002", "audio_sha256": "two"},
        ]
        first["active_take_id"] = "take_001"
        current = director.current_transaction(project, second, voice(), "http://higgs", endpoint())
        second["takes"] = [{
            "id": "take_001", "audio_sha256": "second",
            "transaction": current,
            "human_judgement": {"approved": True, "approved_at": "now", "notes": "good"},
        }]
        second["active_take_id"] = "take_001"
        initial = director.annotate_freshness(project, {region["id"]: director.current_transaction(project, region, voice(), "http://higgs", endpoint()) for region in project["regions"]})
        self.assertTrue(initial["regions"][1]["status"]["approved"])
        self.assertTrue(initial["regions"][1]["status"]["fresh"])
        first["active_take_id"] = "take_002"
        changed = director.annotate_freshness(project, {region["id"]: director.current_transaction(project, region, voice(), "http://higgs", endpoint()) for region in project["regions"]})
        status = changed["regions"][1]["status"]
        self.assertTrue(status["approved"])
        self.assertTrue(status["fresh"])
        self.assertFalse(status["generation_required"])

    def test_continuity_is_diagnostic_but_real_higgs_input_marks_stale(self):
        project, _ = sample_project()
        region = project["regions"][1]
        transaction = director.current_transaction(
            project, region, voice(), "http://higgs", endpoint()
        )
        region["takes"] = [{
            "id": "take_001",
            "audio_sha256": "audio",
            "transaction": transaction,
            "speaker_qa": {"available": True, "speaker_anomaly": False},
            "human_judgement": {"approved": False},
        }]
        region["active_take_id"] = "take_001"
        region["continuity_context"]["previous_regions"] = 0
        annotated = director.annotate_freshness(
            project,
            {item["id"]: director.current_transaction(project, item, voice(), "http://higgs", endpoint()) for item in project["regions"]},
        )
        self.assertTrue(annotated["regions"][1]["status"]["fresh"])

        region["source_text"] += " Промяна."
        region["source_text_sha256"] = director.sha256_text(region["source_text"])
        annotated = director.annotate_freshness(
            project,
            {item["id"]: director.current_transaction(project, item, voice(), "http://higgs", endpoint()) for item in project["regions"]},
        )
        self.assertFalse(annotated["regions"][1]["status"]["fresh"])
        self.assertTrue(annotated["regions"][1]["status"]["generation_required"])

    def test_generation_operations_preserve_exactness_semantics(self):
        project, _ = sample_project()
        current = director.current_transaction(project, project["regions"][0], voice(), "http://higgs", endpoint())
        performance = director.transaction_for_operation(current, "new_performance")
        self.assertNotEqual(performance["higgs_request"]["seed"], current["higgs_request"]["seed"])
        self.assertEqual(performance["freshness_fingerprint"], current["freshness_fingerprint"])
        take = {"id": "take_007", "transaction": performance}
        retry = director.transaction_for_operation(current, "retry_exact", take)
        self.assertEqual(retry["higgs_request"], performance["higgs_request"])
        self.assertEqual(retry["based_on_take_id"], "take_007")
        variation = director.transaction_for_operation(current, "variation")
        self.assertIn("variation", variation)
        self.assertEqual(variation["variation"]["seed_from"], current["higgs_request"]["seed"])
        self.assertEqual(variation["variation"]["seed_to"], variation["higgs_request"]["seed"])
        self.assertEqual(
            variation["variation"]["base_generation_fingerprint"],
            current["generation_fingerprint"],
        )
        self.assertEqual(variation["freshness_fingerprint"], current["freshness_fingerprint"])
        baseline = director.transaction_for_operation(current, "baseline")
        self.assertEqual(baseline["operation"], "baseline")
        self.assertEqual(baseline["higgs_request"]["seed"], current["higgs_request"]["seed"])
        self.assertEqual(baseline["higgs_request"], current["higgs_request"])

    def test_baseline_plan_uses_matching_local_timing_and_missing_active_regions(self):
        project, _ = sample_project()
        first = project["regions"][0]
        transaction = director.current_transaction(project, first, voice(), "http://higgs", endpoint())
        first["takes"] = [{
            "id": "take_001", "duration_seconds": 10.0, "elapsed_seconds": 12.0,
            "transaction": transaction,
        }]
        first["active_take_id"] = "take_001"
        with patch.object(server, "HIGGS_ENDPOINT", "http://higgs"):
            plan = server.baseline_plan(project, {**endpoint(), "model_loaded": True})
        self.assertEqual(plan["target_region_ids"], ["c002", "c003"])
        self.assertEqual(plan["existing_active_regions"], 1)
        self.assertEqual(plan["timing_sample_count"], 1)
        self.assertEqual(plan["confidence"], "rough")
        self.assertGreater(plan["estimated_upper_seconds"], plan["estimated_lower_seconds"])
        self.assertTrue(plan["model_loaded"])

    def test_baseline_activation_is_empty_only_and_rejects_config_drift(self):
        project, _ = sample_project()
        region = project["regions"][0]
        fingerprint = server.baseline_generation_config_fingerprint(project)
        activated, reason = server.activate_baseline_take_if_empty(
            project, region, "take_001", fingerprint
        )
        self.assertTrue(activated)
        self.assertIsNone(reason)
        self.assertEqual(region["active_take_id"], "take_001")
        activated, reason = server.activate_baseline_take_if_empty(
            project, region, "take_002", fingerprint
        )
        self.assertFalse(activated)
        self.assertIsNone(reason)
        self.assertEqual(region["active_take_id"], "take_001")

        other = project["regions"][1]
        project["generation_defaults"]["temperature"] = 1.1
        activated, reason = server.activate_baseline_take_if_empty(
            project, other, "take_001", fingerprint
        )
        self.assertFalse(activated)
        self.assertEqual(reason, "generation_config_changed")
        self.assertIsNone(other["active_take_id"])

    def test_baseline_config_fingerprint_ignores_edl_and_audio_edits(self):
        project, _ = sample_project()
        baseline = server.baseline_generation_config_fingerprint(project)
        project["regions"][0]["takes"] = [{"id": "take_001", "edits": {"fade_in_ms": 80}}]
        project["regions"][0]["active_take_id"] = "take_001"
        project["regions"][0]["pause_after_ms"] = 900
        project["tracks"][0]["output_gain_db"] = -2.0
        self.assertEqual(baseline, server.baseline_generation_config_fingerprint(project))
        project["tracks"][0]["voice_mode"] = "another-voice"
        self.assertNotEqual(baseline, server.baseline_generation_config_fingerprint(project))

    def test_baseline_worker_generates_missing_only_then_renders(self):
        project, lab = sample_project()
        first = project["regions"][0]
        first["takes"] = [{"id": "take_001", "audio_sha256": "keep"}]
        first["active_take_id"] = "take_001"
        fingerprint = server.baseline_generation_config_fingerprint(project)
        generated = []

        def fake_generate(payload):
            region = next(item for item in project["regions"] if item["id"] == payload["region_id"])
            take_id = "take_001"
            region["takes"].append({"id": take_id, "audio_sha256": region["id"]})
            activated, reason = server.activate_baseline_take_if_empty(
                project, region, take_id, payload["baseline_config_fingerprint"]
            )
            generated.append(region["id"])
            return {
                "take": {"id": take_id}, "auto_activated": activated,
                "activation_blocked_reason": reason,
            }

        server.set_baseline_activity(
            replace=True,
            state="queued",
            job_id="baseline-fixture",
            target_regions=2,
            completed_regions=0,
            skipped_regions=0,
            processed_regions=0,
            cancel_requested=False,
        )
        self.addCleanup(
            lambda: server.set_baseline_activity(
                replace=True, state="idle", message="No baseline first-read job is running."
            )
        )
        with (
            patch.object(server, "load_director_project", return_value=(project, lab, [], endpoint())),
            patch.object(server, "generate_director_take", side_effect=fake_generate),
            patch.object(server, "render_director_story", return_value={
                "id": "render-fixture", "audio_url": "/renders/fixture.wav", "duration_seconds": 3.0,
            }) as render,
            patch.object(server, "log_lab_generation"),
        ):
            server._baseline_worker(
                "baseline-fixture", ["c002", "c003"], fingerprint
            )
        self.assertEqual(generated, ["c002", "c003"])
        self.assertEqual(first["active_take_id"], "take_001")
        self.assertEqual(project["regions"][1]["active_take_id"], "take_001")
        self.assertEqual(project["regions"][2]["active_take_id"], "take_001")
        render.assert_called_once_with({})
        activity = server.baseline_activity()
        self.assertEqual(activity["state"], "completed")
        self.assertEqual(activity["completed_regions"], 2)
        self.assertEqual(activity["render_id"], "render-fixture")

    def test_start_baseline_commits_track_voice_and_preserves_region_override(self):
        project, lab = sample_project()
        project["regions"][0]["overrides"]["voice_mode"] = "canon"
        alternate = {
            "id": "alternate", "label": "Alternate anchor",
            "audio_sha256": "alternate-audio", "transcript_sha256": "alternate-text",
        }
        committed = []
        fake_store = SimpleNamespace(
            commit=lambda before, changed, action: (committed.append((before, changed, action)) or (changed, True))
        )
        fake_thread = SimpleNamespace(start=lambda: None)
        server.set_baseline_activity(
            replace=True, state="idle", message="No baseline first-read job is running."
        )
        self.addCleanup(
            lambda: server.set_baseline_activity(
                replace=True, state="idle", message="No baseline first-read job is running."
            )
        )
        with (
            patch.object(server, "load_director_project", return_value=(
                project, lab, [], {**endpoint(), "reachable": True, "model_loaded": True},
            )),
            patch.object(server, "available_voices", return_value=[voice(), alternate]),
            patch.object(server, "DIRECTOR_STORE", fake_store),
            patch.object(server.threading, "Thread", return_value=fake_thread) as thread,
        ):
            activity = server.start_baseline_first_read({"voice_mode": "alternate"})
        self.assertEqual(activity["state"], "queued")
        self.assertEqual(activity["target_regions"], 3)
        self.assertEqual(committed[0][2], "baseline_voice_choice")
        self.assertTrue(all(track["voice_mode"] == "alternate" for track in project["tracks"]))
        self.assertEqual(project["regions"][0]["overrides"]["voice_mode"], "canon")
        thread.assert_called_once()

    def test_render_applies_internal_and_inter_region_pauses_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            with wave.open(str(source), "wb") as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(1000)
                handle.writeframes(b"\1\0" * 1000)
            output = root / "render.wav"
            metadata = director.concatenate_wavs(
                [
                    (source, {"trim_start_ms": 100, "trim_end_ms": 100, "inserted_pauses": [{"at_ms": 400, "duration_ms": 500}]}),
                    (source, {}),
                ],
                [700],
                output,
            )
            self.assertAlmostEqual(metadata["duration_seconds"], 3.0, places=3)
            with wave.open(str(source), "rb") as handle:
                self.assertEqual(handle.getnframes(), 1000)

    def test_dsp_uses_source_timeline_and_applies_gain_fades(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            with wave.open(str(source), "wb") as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(1000)
                handle.writeframes(array("h", [1000] * 1000).tobytes())
            params, frames = director.edited_pcm(
                source,
                {
                    "trim_start_ms": 100, "trim_end_ms": 100,
                    "fade_in_ms": 100, "fade_out_ms": 100,
                    "clip_gain_db": 6.0,
                    "inserted_pauses": [
                        {"at_ms": 50, "duration_ms": 90},
                        {"at_ms": 400, "duration_ms": 50},
                        {"at_ms": 950, "duration_ms": 90},
                    ],
                },
                -6.0,
            )
            samples = array("h"); samples.frombytes(frames)
            self.assertEqual(params.framerate, 1000)
            self.assertEqual(len(samples), 850)
            self.assertEqual(samples[300:350], array("h", [0] * 50))
            self.assertLess(abs(samples[0]), 20)
            self.assertGreater(samples[150], 950)
            self.assertLess(abs(samples[-1]), 20)

    def test_render_fingerprint_tracks_audio_edits_and_gain_not_qa(self):
        project, _ = sample_project()
        region = project["regions"][0]
        region["takes"] = [{"id": "take_001", "audio_sha256": "audio", "edits": {}}]
        region["active_take_id"] = "take_001"
        baseline = director.current_render_fingerprint(project)
        region["takes"][0]["qa_evaluations"] = [{"id": "q1", "state": "fail"}]
        self.assertEqual(baseline, director.current_render_fingerprint(project))
        project["tracks"][0]["output_gain_db"] = -1.5
        gained = director.current_render_fingerprint(project)
        self.assertNotEqual(baseline, gained)
        region["takes"][0]["edits"] = {"fade_in_ms": 120}
        self.assertNotEqual(gained, director.current_render_fingerprint(project))

    def test_speaker_track_assignment_preserves_source_order_and_changes_inheritance(self):
        project, _ = sample_project()
        region = project["regions"][1]
        before = director.current_transaction(project, region, voice(), "http://higgs", endpoint())
        dimitar = next(track for track in project["tracks"] if track["id"] == "dimitar")
        dimitar["voice_mode"] = "dimitar-canon"
        dimitar["delivery_defaults"]["emotion"] = "determination"
        region["track_id"] = "dimitar"
        dimitar_voice = {
            "id": "dimitar-canon", "label": "Dimitar",
            "audio_sha256": "dimitar-audio", "transcript_sha256": "dimitar-text",
        }
        after = director.current_transaction(project, region, dimitar_voice, "http://higgs", endpoint())
        self.assertNotEqual(before["generation_fingerprint"], after["generation_fingerprint"])
        self.assertEqual(after["higgs_request"]["voice_mode"], "dimitar-canon")
        self.assertEqual(after["higgs_request"]["controls"]["emotion"], "determination")
        for item in project["regions"]:
            item["takes"] = [{"id": "take_001", "audio_sha256": item["id"], "edits": {}}]
            item["active_take_id"] = "take_001"
        edl, missing = director.current_edl(project)
        self.assertFalse(missing)
        self.assertEqual([item["region_id"] for item in edl], ["c001", "c002", "c003"])
        self.assertEqual([item["track_id"] for item in edl], ["narrator", "dimitar", "narrator"])

    def test_take_policy_snapshot_not_current_toggle_controls_qa_state(self):
        project, _ = sample_project()
        take = {
            "id": "take_001",
            "qa_policy_snapshot": {"gates": {
                "speaker_similarity": {"enabled": True}, "asr_content": {"enabled": False},
            }},
            "qa_evaluations": [
                {"id": "s", "gate": "speaker_similarity", "state": "pass"},
                {"id": "a", "gate": "asr_content", "state": "skipped"},
            ],
        }
        project["qa_policy"]["speaker_similarity"]["enabled"] = False
        project["qa_policy"]["asr_content"]["enabled"] = True
        status = director.qa_status_for_take(project, take)
        self.assertEqual(status["overall"], "pass")
        self.assertFalse(status["required"])

    def test_generation_qa_and_render_states_are_independent(self):
        project, _ = sample_project()
        region = project["regions"][0]
        transaction = director.current_transaction(
            project, region, voice(), "http://higgs", endpoint()
        )
        region["takes"] = [{
            "id": "take_001",
            "audio_sha256": "audio",
            "transaction": transaction,
            "speaker_qa": {"available": True, "speaker_anomaly": False},
            "human_judgement": {"approved": False},
            "edits": {"trim_start_ms": 0, "trim_end_ms": 0, "inserted_pauses": []},
        }]
        region["active_take_id"] = "take_001"
        transactions = {
            item["id"]: director.current_transaction(project, item, voice(), "http://higgs", endpoint())
            for item in project["regions"]
        }
        annotated = director.annotate_freshness(project, transactions)
        workflow = director.workflow_state(annotated)
        self.assertFalse(annotated["regions"][0]["status"]["generation_required"])
        self.assertTrue(annotated["regions"][0]["status"]["qa_required"])
        self.assertGreater(workflow["generation_required"], 0)  # regions without takes
        self.assertEqual(workflow["qa_required"], 1)
        self.assertTrue(workflow["render_outdated"])

    def test_render_fingerprint_ignores_continuity_but_tracks_edl_pause(self):
        project, _ = sample_project()
        region = project["regions"][0]
        transaction = director.current_transaction(
            project, region, voice(), "http://higgs", endpoint()
        )
        region["takes"] = [{
            "id": "take_001",
            "audio_sha256": "audio",
            "edits": {},
            "transaction": transaction,
        }]
        region["active_take_id"] = "take_001"
        original = director.current_render_fingerprint(project)
        region["continuity_context"]["previous_regions"] = 3
        self.assertEqual(original, director.current_render_fingerprint(project))
        project["renders"] = [{"id": "render_001", "render_fingerprint": original}]
        transactions = {
            item["id"]: director.current_transaction(
                project, item, voice(), "http://higgs", endpoint()
            )
            for item in project["regions"]
        }
        annotated = director.annotate_freshness(project, transactions)
        self.assertFalse(annotated["regions"][0]["status"]["generation_required"])
        self.assertFalse(director.workflow_state(annotated)["render_outdated"])
        region["pause_after_ms"] += 50
        self.assertNotEqual(original, director.current_render_fingerprint(project))
        transactions = {
            item["id"]: director.current_transaction(
                project, item, voice(), "http://higgs", endpoint()
            )
            for item in project["regions"]
        }
        annotated = director.annotate_freshness(project, transactions)
        self.assertFalse(annotated["regions"][0]["status"]["generation_required"])
        self.assertTrue(director.workflow_state(annotated)["render_outdated"])

    def test_schema_migration_preserves_takes_and_adds_v3_state(self):
        project, _ = sample_project()
        project["version"] = 1
        project.pop("qa_policy")
        project.pop("state_meta")
        project["regions"][0]["takes"] = [{"id": "take_001", "audio_sha256": "keep"}]
        migrated, changed = director.migrate_project(project)
        self.assertTrue(changed)
        self.assertEqual(migrated["version"], 3)
        self.assertEqual(migrated["regions"][0]["takes"][0]["audio_sha256"], "keep")
        self.assertTrue(migrated["qa_policy"]["speaker_similarity"]["enabled"])
        self.assertEqual(migrated["tracks"][0]["output_gain_db"], 0.0)
        self.assertIn("volume_envelope", migrated["regions"][0]["takes"][0]["edits"])

    def test_v1_fingerprint_upgrade_ignores_only_changed_audition_context(self):
        project, _ = sample_project()
        first, second = project["regions"][:2]
        first["takes"] = [
            {"id": "take_001", "audio_sha256": "one"},
            {"id": "take_002", "audio_sha256": "two"},
        ]
        first["active_take_id"] = "take_001"
        original = director.current_transaction(
            project, second, voice(), "http://higgs", endpoint()
        )
        original.pop("generation_fingerprint")
        original["freshness_fingerprint"] = original.pop("legacy_freshness_fingerprint")
        second["takes"] = [{"id": "take_001", "transaction": original}]
        second["active_take_id"] = "take_001"
        first["active_take_id"] = "take_002"
        current = {
            region["id"]: director.current_transaction(project, region, voice(), "http://higgs", endpoint())
            for region in project["regions"]
        }
        upgraded, changed = director.upgrade_take_fingerprints(project, current)
        self.assertTrue(changed)
        self.assertEqual(
            upgraded["regions"][1]["takes"][0]["transaction"]["generation_fingerprint"],
            current[second["id"]]["generation_fingerprint"],
        )

    def test_manuscript_edits_preserve_region_ids_boundaries_and_assets(self):
        project, _ = sample_project()
        original_ids = [region["id"] for region in project["regions"]]
        project["regions"][1]["takes"] = [{"id": "take_001", "audio_sha256": "keep"}]
        boundary = project["regions"][1]["start"]
        token = "<|emotion:amusement|>"
        edited = project["manuscript"][:boundary] + token + project["manuscript"][boundary:]
        updated = director.reconcile_manuscript(
            project, edited, active_region_id="c002"
        )
        self.assertEqual([region["id"] for region in updated["regions"]], original_ids)
        self.assertNotIn(token, updated["regions"][0]["source_text"])
        self.assertTrue(updated["regions"][1]["source_text"].startswith(token))
        self.assertEqual(updated["regions"][1]["takes"][0]["audio_sha256"], "keep")
        self.assertEqual(updated["regions"][0]["start"], 0)
        self.assertEqual(updated["regions"][-1]["end"], len(edited))
        self.assertTrue(all(
            left["end"] == right["start"]
            for left, right in zip(updated["regions"], updated["regions"][1:])
        ))

    def test_boundary_insert_is_owned_by_the_active_adjacent_region(self):
        project, _ = sample_project()
        boundary = project["regions"][1]["start"]
        insertion = "TAG"
        edited = project["manuscript"][:boundary] + insertion + project["manuscript"][boundary:]
        owned_left = director.reconcile_manuscript(
            project, edited, active_region_id="c001"
        )
        owned_right = director.reconcile_manuscript(
            project, edited, active_region_id="c002"
        )
        self.assertTrue(owned_left["regions"][0]["source_text"].endswith(insertion))
        self.assertTrue(owned_right["regions"][1]["source_text"].startswith(insertion))

    def test_inline_direction_stales_only_its_stable_region(self):
        project, _ = sample_project()
        for region in project["regions"]:
            transaction = director.current_transaction(
                project, region, voice(), "http://higgs", endpoint()
            )
            region["takes"] = [{
                "id": "take_001",
                "audio_sha256": region["id"],
                "transaction": transaction,
                "speaker_qa": {"available": True, "speaker_anomaly": False},
                "asr_qa": {"state": "pass"},
                "human_judgement": {"approved": False},
            }]
            region["active_take_id"] = "take_001"
        boundary = project["regions"][1]["start"]
        token = "<|emotion:amusement|>"
        edited = project["manuscript"][:boundary] + token + project["manuscript"][boundary:]
        updated = director.reconcile_manuscript(
            project, edited, active_region_id="c002"
        )
        transactions = {
            region["id"]: director.current_transaction(
                updated, region, voice(), "http://higgs", endpoint()
            )
            for region in updated["regions"]
        }
        annotated = director.annotate_freshness(updated, transactions)
        self.assertEqual(
            [region["status"]["generation_required"] for region in annotated["regions"]],
            [False, True, False],
        )

    def test_edit_that_erases_a_region_is_rejected(self):
        project, _ = sample_project()
        middle = project["regions"][1]
        edited = (
            project["manuscript"][:middle["start"]]
            + project["manuscript"][middle["end"]:]
        )
        with self.assertRaisesRegex(ValueError, "erase or merge"):
            director.reconcile_manuscript(project, edited, active_region_id="c002")


class ProjectStoreTests(unittest.TestCase):
    def test_persistent_undo_redo_and_recovery_preserve_generated_takes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.json"
            store = ProjectStore(path, history_limit=3)
            project, _ = sample_project()
            region = project["regions"][0]
            region["takes"] = [
                {"id": "take_001", "audio_sha256": "one"},
                {"id": "take_002", "audio_sha256": "two"},
            ]
            region["active_take_id"] = "take_001"
            current = store.initialize(project)

            changed = deepcopy(current)
            changed["regions"][0]["active_take_id"] = "take_002"
            current, committed = store.commit(current, changed, "take_activate")
            self.assertTrue(committed)

            changed = deepcopy(current)
            changed["regions"][0]["pause_after_ms"] = 1250
            current, committed = store.commit(current, changed, "boundary_pause")
            self.assertTrue(committed)

            reloaded = ProjectStore(path, history_limit=3)
            current = reloaded.load()
            self.assertEqual(reloaded.status()["cursor"], 2)
            current, undone = reloaded.undo(current)
            self.assertTrue(undone)
            self.assertNotEqual(current["regions"][0]["pause_after_ms"], 1250)

            generated = deepcopy(current)
            generated["regions"][0]["takes"].append(
                {"id": "take_003", "audio_sha256": "three"}
            )
            current = reloaded.write_system(generated, "generate_take")
            current, undone = reloaded.undo(current)
            self.assertTrue(undone)
            self.assertEqual(current["regions"][0]["active_take_id"], "take_001")
            self.assertIn("take_003", {take["id"] for take in current["regions"][0]["takes"]})

            current, redone = reloaded.redo(current)
            self.assertTrue(redone)
            self.assertEqual(current["regions"][0]["active_take_id"], "take_002")

            path.write_text("{broken", encoding="utf-8")
            recovery_store = ProjectStore(path, history_limit=3)
            recovered = recovery_store.load()
            self.assertEqual(recovered["regions"][0]["active_take_id"], "take_002")
            self.assertIn("recovered", recovery_store.last_warning.lower())

    def test_undo_preserves_append_only_qa_and_export_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.json"
            store = ProjectStore(path)
            project, _ = sample_project()
            project["regions"][0]["takes"] = [{
                "id": "take_001", "audio_sha256": "one",
                "qa_evaluations": [], "approval_events": [],
            }]
            current = store.initialize(project)
            changed = deepcopy(current); changed["regions"][0]["pause_after_ms"] = 900
            current, _ = store.commit(current, changed, "boundary_pause")
            system = deepcopy(current)
            system["regions"][0]["takes"][0]["qa_evaluations"].append({"id": "qa-1", "state": "pass"})
            system["regions"][0]["takes"][0]["approval_events"].append({"id": "approval-1"})
            system["export_revisions"].append({"id": "r001"})
            current = store.write_system(system, "qa_and_export")
            restored, _ = store.undo(current)
            take = restored["regions"][0]["takes"][0]
            self.assertEqual([item["id"] for item in take["qa_evaluations"]], ["qa-1"])
            self.assertEqual([item["id"] for item in take["approval_events"]], ["approval-1"])
            self.assertEqual([item["id"] for item in restored["export_revisions"]], ["r001"])

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.json"
            store = ProjectStore(path, history_limit=3)
            project, _ = sample_project()
            current = store.initialize(project)
            for pause in (800, 900, 1000, 1100):
                changed = deepcopy(current)
                changed["regions"][0]["pause_after_ms"] = pause
                current, committed = store.commit(current, changed, "boundary_pause")
                self.assertTrue(committed)
            self.assertEqual(store.status()["depth"], 3)
            self.assertEqual(store.status()["cursor"], 3)

    def test_migration_backup_is_written_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "project.json"
            v1, _ = sample_project()
            v1["version"] = 1
            path.write_text("{}", encoding="utf-8")
            migrated, _ = director.migrate_project(v1)
            ProjectStore(path).initialize(migrated, migration_source=v1)
            backup = path.with_name("project.schema-v1.backup.json")
            self.assertTrue(backup.is_file())
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), v1)


class RenderExportContractTests(unittest.TestCase):
    def test_approval_requires_reason_until_snapshotted_qa_passes(self):
        project, lab = sample_project()
        take = {
            "id": "take_001", "qa_policy_snapshot": {"gates": {
                "speaker_similarity": {"enabled": True}, "asr_content": {"enabled": True},
            }},
            "qa_evaluations": [
                {"id": "speaker-pending", "gate": "speaker_similarity", "state": "pending"},
                {"id": "asr-pass", "gate": "asr_content", "state": "pass"},
            ],
            "approval_events": [],
            "human_judgement": {"approved": False, "approved_at": None, "notes": ""},
        }
        project["regions"][0]["takes"] = [take]
        fake_store = SimpleNamespace(commit=lambda _before, changed, _action: (changed, True))
        with (
            patch.object(server, "load_director_project", return_value=(project, lab, [], endpoint())),
            patch.object(server, "DIRECTOR_STORE", fake_store),
            patch.object(server, "director_state_payload", return_value={"saved": True}),
        ):
            with self.assertRaisesRegex(ValueError, "explicit approval override"):
                server.update_take_decision({
                    "region_id": "c001", "take_id": "take_001", "action": "approve", "approved": True,
                })
            server.update_take_decision({
                "region_id": "c001", "take_id": "take_001", "action": "approve", "approved": True,
                "override_reason": "Human listened and accepts the clip.",
            })
        self.assertTrue(take["human_judgement"]["approved"])
        self.assertEqual(take["approval_events"][-1]["override_reason"], "Human listened and accepts the clip.")

    def test_complete_edl_render_cache_and_immutable_export_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "takes").mkdir()
            project, _ = sample_project()
            canonical_voice = voice()
            for index, region in enumerate(project["regions"], 1):
                source = root / "takes" / f"{region['id']}.wav"
                with wave.open(str(source), "wb") as handle:
                    handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24_000)
                    handle.writeframes(array("h", [index * 500] * 2400).tobytes())
                transaction = director.current_transaction(project, region, canonical_voice, "http://higgs", endpoint())
                take = {
                    "id": "take_001", "audio_file": f"takes/{source.name}",
                    "audio_sha256": server.file_sha256(source), "duration_seconds": .1,
                    "transaction": transaction, "edits": director.normalize_edits({}),
                    "qa_policy_snapshot": {"gates": {
                        "speaker_similarity": {"enabled": True}, "asr_content": {"enabled": True},
                    }},
                    "qa_evaluations": [
                        {"id": f"speaker-{index}", "gate": "speaker_similarity", "state": "pass"},
                        {"id": f"asr-{index}", "gate": "asr_content", "state": "pass"},
                    ],
                    "approval_events": [{"id": f"approval-{index}", "approved": True, "override_reason": None}],
                    "human_judgement": {"approved": True, "approved_at": "now", "notes": ""},
                }
                region["takes"] = [take]; region["active_take_id"] = take["id"]
            store = ProjectStore(root / "project.json")
            store.initialize(project)

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"ID3-directors-room-fixture")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(server, "APP_DIR", root),
                patch.object(server, "RENDER_DIR", root / "renders"),
                patch.object(server, "EXPORT_DIR", root / "exports"),
                patch.object(server, "DIRECTOR_STORE", store),
                patch.object(server, "HIGGS_ENDPOINT", "http://higgs"),
                patch.object(server, "available_voices", return_value=[canonical_voice]),
                patch.object(server.subprocess, "run", side_effect=fake_ffmpeg),
            ):
                first = server.render_director_story({})
                second = server.render_director_story({})
                self.assertFalse(first["cached"])
                self.assertTrue(second["cached"])
                self.assertEqual(first["id"], second["id"])
                preflight = server.export_preflight({"render_id": first["id"]})
                self.assertFalse(preflight["errors"])
                self.assertFalse(preflight["warnings"])
                overridden = store.load()
                overridden["regions"][0]["takes"][0]["approval_events"][-1]["override_reason"] = "Human override"
                store.write_system(overridden, "approval_override_fixture")
                warned = server.export_preflight({"render_id": first["id"]})
                self.assertIn("approval_override", {item["code"] for item in warned["warnings"]})
                cleaned = store.load()
                cleaned["regions"][0]["takes"][0]["approval_events"][-1]["override_reason"] = None
                store.write_system(cleaned, "approval_override_fixture_reset")
                preflight = server.export_preflight({"render_id": first["id"]})
                exported = server.export_director_render({
                    "render_id": first["id"], "preflight_fingerprint": preflight["preflight_fingerprint"],
                    "label": "Sunday master", "notes": "Fixture note",
                })
                orphaned = store.load()
                orphaned["export_revisions"] = []
                orphaned["latest_export_revision_id"] = None
                store.write_system(orphaned, "simulate_post_rename_commit_loss")
                reused = server.export_director_render({
                    "render_id": first["id"], "preflight_fingerprint": preflight["preflight_fingerprint"],
                    "label": "Sunday master", "notes": "Fixture note",
                })
                (root / "takes" / "c001.wav").unlink()
                with self.assertRaisesRegex(ValueError, "Immutable audio"):
                    server.render_director_story({})
                invalid = server.export_preflight({"render_id": first["id"]})
                self.assertIn("source_audio_invalid", {item["code"] for item in invalid["errors"]})
            self.assertEqual(exported["id"], "r001")
            self.assertTrue(reused["reused"])
            self.assertTrue(store.load()["export_revisions"][0]["recovered_from_disk"])
            manifest = json.loads((root / "exports" / project["project_id"] / "r001" / "manifest.json").read_text())
            self.assertEqual(manifest["label"], "Sunday master")
            self.assertEqual(manifest["renderer_build"], "pcm-edl-v2")
            self.assertEqual(manifest["edl"][0]["qa"]["overall"], "pass")
            self.assertTrue(manifest["edl"][0]["approval"]["approved"])
            self.assertTrue((root / "exports" / project["project_id"] / "latest.json").is_file())


if __name__ == "__main__":
    unittest.main()
