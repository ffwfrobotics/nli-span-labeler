"""
Exports (FR-45..49), the FR-5 round trip, model pseudo-labelers (FR-9), gold
(FR-29) and α from the database (FR-37/38).
"""
import json
import random
from pathlib import Path

import jsonschema
import krippendorff
import numpy as np
import pytest
from fastapi.testclient import TestClient

from e13_labeler.reasons import REASONS
from tests.conftest import make_labeler

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_pool.jsonl"
SAMPLE = Path(__file__).parent.parent / "docs" / "e13" / "fixtures" / "pool_eval_libre_sample.jsonl"
SCHEMAS = Path(__file__).parent.parent / "e13_labeler" / "schemas"
FEVER = "fever/eval/5#tall"
FEVER_STATE = "The tower was completed in 1889 and is 330 metres tall."
Y0 = FEVER_STATE.index("1889")
H0 = FEVER_STATE.index("330")


def schema(name):
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


@pytest.fixture
def pilot(db):
    from e13_labeler.batches import set_status
    from e13_labeler.db import get_db
    from e13_labeler.importer import import_file

    with get_db() as conn:
        import_file(conn, SYNTHETIC, batch="pilot")
        set_status(conn, "pilot", "open")


def annotate(item_id, labeler_id, batch="pilot", reasons=(), answerable=None, spans=(), skipped=None,
             gold_probe=False, version=1, created_at=None, note=None):
    """Write an annotation directly (the labelling API has its own tests)."""
    from e13_labeler.db import get_db
    from e13_labeler.labelling import reasons_json

    with get_db() as conn:
        b = conn.execute("SELECT id, reason_set_json FROM batches WHERE name = ?", (batch,)).fetchone()
        reason_set = json.loads(b["reason_set_json"])
        if answerable is None:
            answerable = not reasons
        cur = conn.execute(
            """INSERT INTO annotations (item_id, batch_id, labeler_id, version, answerable, reasons_json, note,
                                        skipped_code, is_gold_probe, active_ms, wall_ms, asof, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1000, 2000, '2026-10-05', COALESCE(?, CURRENT_TIMESTAMP))""",
            (item_id, b["id"], labeler_id, version, None if skipped else int(answerable),
             None if skipped else json.dumps(reasons_json(list(reasons), reason_set)), note, skipped,
             int(gold_probe), created_at))
        for s in spans:
            conn.execute(
                """INSERT INTO spans (annotation_id, side, option, pointer, start, "end", text, role, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cur.lastrowid, s.get("side", "state"), s.get("option"), s.get("pointer"), s.get("start"),
                 s.get("end"), s["text"], s["role"], json.dumps(s.get("reasons", []))))
        return cur.lastrowid


def span(text, role="support", option="true", reasons=()):
    start = FEVER_STATE.index(text)
    return {"text": text, "start": start, "end": start + len(text), "role": role, "option": option,
            "reasons": list(reasons)}


@pytest.fixture
def labelled(pilot):
    """Two humans on fever (one item), three on bbc; L01 is the owner-like first labeler."""
    a, b, c = make_labeler("a", clearance="internal"), make_labeler("b", clearance="internal"), \
        make_labeler("c", clearance="internal")
    stale = [span("1889", reasons=["stale_state"]), span("330", role="support")]
    annotate(FEVER, a["id"], reasons=["stale_state"], spans=stale, note="old figure")
    annotate(FEVER, b["id"], answerable=True, spans=[span("330", role="support")])
    annotate("bbc_news/eval/73#q0", a["id"], reasons=["not_enough_info"])
    annotate("bbc_news/eval/73#q0", b["id"], reasons=["not_enough_info", "ambiguous"])
    annotate("bbc_news/eval/73#q0", c["id"], answerable=True)
    annotate("mystery_source/eval/1#q", c["id"], skipped="broken_item")
    return a, b, c


def export(**kwargs):
    from e13_labeler.db import get_db
    from e13_labeler.exports import write_export
    from e13_labeler.records import Filters

    filters = kwargs.pop("filters", Filters())
    with get_db() as conn:
        return write_export(conn, filters=filters, **kwargs)


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


# ============================================================================
# FR-45 annotations
# ============================================================================

class TestAnnotationExport:
    def test_rows_validate_against_the_shipped_schema(self, labelled, tmp_path):
        manifest = export(out_root=tmp_path, kinds=["annotations"])
        rows = read_jsonl(Path(manifest["directory"]) / "annotations.jsonl")
        assert len(rows) == 6
        validator = schema("annotation")
        for row in rows:
            validator.validate(row)
        assert {r["labeler"] for r in rows} == {"L01", "L02", "L03"}  # pseudonyms only
        skipped = [r for r in rows if r["skipped"]]
        assert skipped == [r for r in rows if r["reasons"] is None and r["answerable"] is None]
        fever = [r for r in rows if r["item_id"] == FEVER and r["labeler"] == "L01"][0]
        assert fever["reasons"]["stale_state"] is True and fever["asof"] == "2026-10-05"
        assert fever["timing"] == {"active_ms": 1000, "wall_ms": 2000} and fever["note"] == "old figure"

    def test_spans_are_slices(self, labelled, tmp_path):
        """FR-17 on export: every span's text is its slice of the state."""
        manifest = export(out_root=tmp_path, kinds=["annotations"])
        for row in read_jsonl(Path(manifest["directory"]) / "annotations.jsonl"):
            for s in row["spans"]:
                assert FEVER_STATE[s["start"]:s["end"]] == s["text"]

    def test_latest_version_only(self, labelled, tmp_path):
        from e13_labeler.db import get_db

        a = labelled[0]
        annotate(FEVER, a["id"], answerable=True, version=2)
        manifest = export(out_root=tmp_path, kinds=["annotations"])
        rows = [r for r in read_jsonl(Path(manifest["directory"]) / "annotations.jsonl")
                if r["item_id"] == FEVER and r["labeler"] == "L01"]
        assert [(r["version"], r["answerable"]) for r in rows] == [(2, True)]
        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM annotations WHERE item_id = ?", (FEVER,)).fetchone()[0] == 3

    def test_filters(self, labelled, tmp_path):
        """FR-47: permissions (exact values), batch, date range, clearance."""
        from e13_labeler.records import Filters

        def rows(**f):
            m = export(out_root=tmp_path, kinds=["annotations"], filters=Filters(**f))
            return read_jsonl(Path(m["directory"]) / "annotations.jsonl")

        assert {r["permissions"] for r in rows(permissions=["jev"])} == {"jev"}  # fever has a Jev answer
        assert rows(permissions=["libre"]) == []
        assert len(rows(batches=["pilot"])) == 6 and rows(batches=["nope"]) == []
        assert rows(until="2000-01-01") == [] and len(rows(since="2000-01-01")) == 6
        # a public exporter gets only libre-visible items: fever (jev, libre text), not bbc or mystery
        assert {r["item_id"] for r in rows(clearance="public")} == {FEVER}
        with pytest.raises(ValueError, match="unknown permissions"):
            rows(permissions=["public"])


# ============================================================================
# FR-46 training
# ============================================================================

class TestTrainingExport:
    def rows(self, tmp_path, **kwargs):
        m = export(out_root=tmp_path, kinds=["training"], **kwargs)
        return {f"{r['id']}#{r['qid']}": r for r in read_jsonl(Path(m["directory"]) / "training.jsonl")}

    def test_soft_targets_majority_and_spans(self, labelled, tmp_path):
        from e13_labeler.batches import configure
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "pilot", overlap_target=2)
        rows = self.rows(tmp_path)
        assert set(rows) == {FEVER, "bbc_news/eval/73#q0"}  # mystery has only a skip
        validator = schema("train")
        for row in rows.values():
            validator.validate(row)

        fever = rows[FEVER]
        abstain = fever["targets"]["mbnli"]["abstain"]
        assert abstain["p"] == 0.5 and abstain["reasons"]["stale_state"] == 0.5
        assert abstain["reasons"]["unrelated"] == 0.0
        assert fever["human"]["majority"] == {"answerable": None, "reasons": [], "tied_reasons": ["stale_state"]}
        assert [(e["text"], e["reasons"], e["votes"]) for e in abstain["evidence"]] == [("1889", ["stale_state"], 1)]
        assert [(e["text"], e["votes"]) for e in fever["targets"]["mbnli"]["evidence"]] == [("330", 2)]
        assert fever["state"] == FEVER_STATE and fever["text_included"] is True
        assert fever["permissions"] == "jev" and fever["source_license"] == "CC-BY-SA-3.0"

        bbc = rows["bbc_news/eval/73#q0"]["human"]
        assert bbc["n_labelers"] == 3 and bbc["majority"]["answerable"] is False
        assert bbc["majority"]["reasons"] == ["not_enough_info"]
        assert rows["bbc_news/eval/73#q0"]["targets"]["mbnli"]["abstain"]["reasons"]["ambiguous"] == pytest.approx(1 / 3, abs=1e-6)

    def test_overlap_target_respected(self, labelled, tmp_path):
        """Default overlap 3: only bbc (3 labelers) qualifies."""
        assert set(self.rows(tmp_path)) == {"bbc_news/eval/73#q0"}

    def test_no_text(self, labelled, tmp_path):
        from e13_labeler.batches import configure
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "pilot", overlap_target=2)
        for row in self.rows(tmp_path, text_included=False).values():
            schema("train").validate(row)
            assert "state" not in row and "question" not in row and row["state_sha256"].startswith("sha256:")

    def test_gold_probes_relabels_models_and_gold_items_excluded(self, labelled, tmp_path):
        from e13_labeler.batches import configure, create_relabel
        from e13_labeler.db import get_db
        from e13_labeler.gold import save_gold
        from e13_labeler.model_labels import import_model_labels

        a, b, c = labelled
        with get_db() as conn:
            configure(conn, "pilot", overlap_target=1)
            create_relabel(conn, "pilot", "pilot-r2", after_days=0)
            import_model_labels(conn, [json.dumps({"labeler_kind": "model", "labeler": "committee_prompt_a",
                                                   "item_id": FEVER, "reasons": {"unrelated": True}})], "m")
            save_gold(conn, "bbc_news/eval/73#q0", reasons=["not_enough_info"])
        annotate(FEVER, a["id"], batch="pilot-r2", reasons=["subjective"])
        annotate("typed_decisions/security_incidents_000085#severity", c["id"], answerable=True, gold_probe=True)
        rows = self.rows(tmp_path)
        assert set(rows) == {FEVER}
        assert rows[FEVER]["human"]["labelers"] == ["L01", "L02"]  # no model, no re-label pass
        assert rows[FEVER]["targets"]["mbnli"]["abstain"]["reasons"]["subjective"] == 0.0
        from e13_labeler.records import Filters

        assert "bbc_news/eval/73#q0" in self.rows(tmp_path, filters=Filters(include_gold=True))

    def test_reason_keys_follow_the_reason_set(self, pilot, tmp_path):
        """FR-33/§5.5: reasons outside the batch's reason set export as null, not 0."""
        from e13_labeler.batches import configure
        from e13_labeler.db import get_db

        with get_db() as conn:
            configure(conn, "pilot", overlap_target=1)
            conn.execute("UPDATE batches SET reason_set_json = ?", (json.dumps(list(REASONS[:8])),))
        annotate(FEVER, make_labeler("a")["id"], reasons=["ambiguous"])
        row = self.rows(tmp_path)[FEVER]
        assert row["targets"]["mbnli"]["abstain"]["reasons"]["subjective"] is None
        assert row["targets"]["mbnli"]["abstain"]["reasons"]["ambiguous"] == 1.0
        assert row["human"]["reason_set"] == list(REASONS[:8])


# ============================================================================
# FR-5 round trip
# ============================================================================

class TestRoundTrip:
    def roundtrip(self, source, tmp_path, monkeypatch):
        from e13_labeler.db import get_db, init_db
        from e13_labeler.importer import import_file

        with get_db() as conn:
            import_file(conn, source, batch="rt")
            before = {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
        manifest = export(out_root=tmp_path / "exp", kinds=["items"])
        items_file = Path(manifest["directory"]) / "items.jsonl"

        with get_db() as conn:  # re-import into the same database: all unchanged
            again = import_file(conn, items_file)
        assert again.n_rejected == 0 and again.n_items == 0 and again.n_unchanged == len(before)

        monkeypatch.setenv("E13_DB", str(tmp_path / "fresh.db"))  # and into a fresh one: identical
        init_db()
        with get_db() as conn:
            fresh = import_file(conn, items_file)
            after = {r["item_id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
        assert fresh.n_rejected == 0 and set(after) == set(before)
        for item_id, b in before.items():
            a = after[item_id]
            assert a["state"].encode() == b["state"].encode(), item_id  # byte-identical
            for col in ("state_sha256", "state_format", "question_json", "gold_json", "permissions", "visibility",
                        "source", "split", "heldout", "e13_json", "model_answers_json", "source_license"):
                assert a[col] == b[col], (item_id, col)

    def test_synthetic(self, db, tmp_path, monkeypatch):
        """FR-5: export -> re-import gives byte-identical states and hashes (JSON, object and text states)."""
        self.roundtrip(SYNTHETIC, tmp_path, monkeypatch)

    @pytest.mark.skipif(not SAMPLE.exists(), reason="pool sample not present")
    def test_pool_sample(self, db, tmp_path, monkeypatch):
        self.roundtrip(SAMPLE, tmp_path, monkeypatch)

    def test_annotation_hashes_match_items(self, labelled, tmp_path):
        """Exported annotations point at the state by hash; the hash is the item's."""
        from e13_labeler.importer import sha256_text

        m = export(out_root=tmp_path, kinds=["annotations"])
        for r in read_jsonl(Path(m["directory"]) / "annotations.jsonl"):
            if r["item_id"] == FEVER:
                assert r["state_sha256"] == sha256_text(FEVER_STATE)


# ============================================================================
# FR-48 agreement, FR-49 manifest
# ============================================================================

class TestAgreementAndManifest:
    def test_agreement_reproduces_offline(self, labelled, tmp_path):
        from e13_labeler.analysis import report

        m = export(out_root=tmp_path, kinds=["agreement"], n_boot=200, seed=3)
        doc = json.loads((Path(m["directory"]) / "agreement.json").read_text())
        again = report(doc["data"], n_boot=200, seed=3)
        assert json.loads(json.dumps(again)) == {k: v for k, v in doc.items() if k not in ("filters", "data")}
        assert doc["inter_rater"]["n_items_pairable"] == 2

    def test_manifest(self, labelled, tmp_path):
        from e13_labeler.db import get_db
        from e13_labeler.exports import db_checksum

        with get_db() as conn:
            before = db_checksum(conn)
        m = export(out_root=tmp_path)
        d = Path(m["directory"])
        assert {f["name"] for f in m["files"]} == {"annotations.jsonl", "training.jsonl", "agreement.json",
                                                   "items.jsonl"}
        on_disk = json.loads((d / "manifest.json").read_text())
        assert on_disk == m and m["db_sha256"] == before and m["app_version"]
        import hashlib

        for f in m["files"]:
            assert f["sha256"] == "sha256:" + hashlib.sha256((d / f["name"]).read_bytes()).hexdigest()
        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'export'").fetchone()[0] == 1

    def test_never_overwrites(self, labelled, tmp_path):
        dirs = {export(out_root=tmp_path, kinds=["annotations"])["directory"] for _ in range(3)}
        assert len(dirs) == 3 and all(Path(d).exists() for d in dirs)

    def test_no_text_refuses_items(self, labelled, tmp_path):
        with pytest.raises(ValueError, match="state text"):
            export(out_root=tmp_path, kinds=["items"], text_included=False)

    def test_cli(self, labelled, tmp_path, capsys):
        from e13_labeler.__main__ import main

        assert main(["export", "--out", str(tmp_path), "--kind", "annotations", "--kind", "agreement",
                     "--n-boot", "50"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [f["name"] for f in out["files"]] == ["annotations.jsonl", "agreement.json"]
        assert main(["agreement", "--n-boot", "50"]) == 0
        assert "Inter-rater" in capsys.readouterr().out


# ============================================================================
# FR-37/38 α from the database
# ============================================================================

class TestAlphaFromDb:
    def records(self, **f):
        from e13_labeler.analysis import agreement_record
        from e13_labeler.db import get_db
        from e13_labeler.records import Filters, load_annotations

        with get_db() as conn:
            return [agreement_record(r) for r in load_annotations(conn, Filters(**f))]

    def test_matches_reference_on_simulated_labels(self, db):
        """FR-37: per-reason α from the DB equals the krippendorff package on the same data."""
        from e13_labeler.analysis import report
        from e13_labeler.batches import configure
        from e13_labeler.db import get_db
        from e13_labeler.importer import import_rows

        rows = [json.dumps({"id": f"snli/s/{i}", "source": "snli", "state": f"s{i}",
                            "questions": {"q": {"type": "noul"}}}) for i in range(60)]
        with get_db() as conn:
            import_rows(conn, rows, "g", "x", batch="pilot")
            configure(conn, "pilot", overlap_target=3)
        labelers = [make_labeler(f"l{i}")["id"] for i in range(3)]
        rng = random.Random(5)
        truth = {}
        for i in range(60):
            truth[i] = rng.random() < 0.3
            for lab in labelers:
                if rng.random() < 0.15:
                    continue  # missing
                value = truth[i] if rng.random() < 0.8 else not truth[i]
                annotate(f"snli/s/{i}#q", lab, reasons=["ambiguous"] if value else ["not_enough_info"])
        rep = report(self.records(), n_boot=0)
        matrix = np.full((3, 60), np.nan)
        for r in self.records():
            col = int(r["item_id"].split("/")[2].split("#")[0])
            matrix[["L01", "L02", "L03"].index(r["labeler"]), col] = r["reasons"]["ambiguous"]
        expected = krippendorff.alpha(reliability_data=matrix, level_of_measurement="nominal")
        assert rep["inter_rater"]["per_reason"]["ambiguous"]["alpha"] == pytest.approx(expected, abs=1e-6)

    def test_exclusions_and_sections(self, labelled):
        """Gold probes and non-blind batches stay out; re-labels are intra-rater; models are separate."""
        from e13_labeler.analysis import report
        from e13_labeler.batches import create_relabel
        from e13_labeler.db import get_db
        from e13_labeler.model_labels import import_model_labels

        a, b, c = labelled
        with get_db() as conn:
            create_relabel(conn, "pilot", "pilot-r2", after_days=0)
            conn.execute("INSERT INTO batches (name, reason_set_json, show_model_answer) VALUES ('audit', ?, 'jev')",
                         (json.dumps(list(REASONS)),))
            conn.execute("INSERT INTO batch_items (batch_id, item_id) SELECT id, ? FROM batches WHERE name='audit'",
                         ("typed_decisions/security_incidents_000085#severity",))
            lines = [json.dumps({"labeler_kind": "model", "labeler": m, "item_id": i, "reasons": {"not_enough_info": True}})
                     for m in ("committee_prompt_a", "committee_prompt_b") for i in (FEVER, "bbc_news/eval/73#q0")]
            import_model_labels(conn, lines, "m")
        annotate(FEVER, a["id"], batch="pilot-r2", reasons=["stale_state"],
                 spans=[span("1889", reasons=["stale_state"])])
        annotate("typed_decisions/security_incidents_000085#severity", a["id"], batch="audit", answerable=True)
        annotate("typed_decisions/security_incidents_000085#severity", b["id"], batch="audit", reasons=["subjective"])
        annotate("snli/eval/12#outdoors", a["id"], answerable=True, gold_probe=True)
        annotate("snli/eval/12#outdoors", b["id"], reasons=["unrelated"], gold_probe=True)

        rep = report(self.records(), n_boot=0)
        assert rep["inter_rater"]["n_items_pairable"] == 2  # fever and bbc only
        assert rep["intra_rater"]["n_pairs"] == 1
        assert rep["intra_rater"]["per_reason"]["stale_state"]["n_items"] == 1
        assert set(rep["human_vs_model"]) == {"committee_prompt_a", "committee_prompt_b"}
        assert rep["human_vs_model"]["committee_prompt_a"]["n_pairs"] == 5  # 2 humans on fever + 3 on bbc
        assert rep["model_vs_model"]["committee_prompt_a|committee_prompt_b"]["per_reason"]["not_enough_info"]["n_items"] == 2
        assert set(rep["inter_rater"]["candidates"]) == {"stale_state", "subjective"}


# ============================================================================
# FR-9 model pseudo-labelers
# ============================================================================

class TestModelLabels:
    def run(self, lines):
        from e13_labeler.db import get_db
        from e13_labeler.model_labels import import_model_labels

        with get_db() as conn:
            return import_model_labels(conn, [json.dumps(x) if isinstance(x, dict) else x for x in lines], "m")

    def test_import_and_idempotency(self, pilot):
        row = {"schema": "e13.annotation/1", "labeler_kind": "model", "labeler": "committee_prompt_a",
               "item_id": FEVER, "batch": "pilot", "answerable": False,
               "reasons": {"stale_state": True, "unrelated": False},
               "spans": [{"side": "state", "start": Y0, "end": Y0 + 4, "text": "1889", "role": "support",
                          "option": "true", "reasons": ["stale_state"]}]}
        first = self.run([row])
        assert first.n_imported == 1 and first.labelers == ["committee_prompt_a"]
        assert self.run([row]).n_duplicate == 1
        from e13_labeler.db import get_db

        with get_db() as conn:
            lab = conn.execute("SELECT * FROM labelers WHERE pseudonym = 'committee_prompt_a'").fetchone()
            assert (lab["kind"], lab["role"], lab["login_name"]) == ("model", "model", None)
            assert conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 1

    @pytest.mark.parametrize("row, message", [
        ({"labeler_kind": "human", "labeler": "x", "item_id": FEVER}, "labeler_kind"),
        ({"labeler_kind": "model", "labeler": "x", "item_id": "nope#q"}, "unknown item"),
        ({"labeler_kind": "model", "labeler": "x y", "item_id": FEVER}, "must match"),
        ({"labeler_kind": "model", "labeler": "x", "item_id": FEVER, "reasons": {"boring": True}}, "reasons must"),
        ({"labeler_kind": "model", "labeler": "x", "item_id": FEVER, "answerable": True,
          "reasons": {"ambiguous": True}}, "answerable excludes"),
        ({"labeler_kind": "model", "labeler": "x", "item_id": FEVER, "reasons": {"stale_state": True},
          "spans": [{"side": "state", "start": 0, "end": 4, "text": "1889", "role": "support"}]}, "slice"),
    ])
    def test_rejections(self, pilot, row, message):
        report = self.run([row])
        assert report.n_rejected == 1 and message in report.errors[0][1]

    def test_human_pseudonym_refused(self, pilot):
        make_labeler("h")
        report = self.run([{"labeler_kind": "model", "labeler": "L01", "item_id": FEVER}])
        assert "human pseudonym" in report.errors[0][1]

    def test_jev_labels_raise_the_row_release_tier(self, pilot):
        from e13_labeler.db import get_db

        item = "typed_decisions/security_incidents_000085#severity"
        self.run([{"labeler_kind": "model", "labeler": "jev", "item_id": item, "answerable": True}])
        with get_db() as conn:
            got = conn.execute("SELECT permissions, visibility FROM items WHERE row_id = ?",
                               ("typed_decisions/security_incidents_000085",)).fetchall()
        assert {tuple(r) for r in got} == {("jev", "libre")}

    def test_cli(self, pilot, tmp_path, capsys):
        from e13_labeler.__main__ import main

        f = tmp_path / "labels.jsonl"
        f.write_text(json.dumps({"labeler_kind": "model", "labeler": "m1", "item_id": FEVER, "answerable": True}) + "\n")
        assert main(["import-labels", str(f)]) == 0
        assert json.loads(capsys.readouterr().out)["n_imported"] == 1


# ============================================================================
# FR-29 gold
# ============================================================================

class TestGold:
    def test_crud_api(self, pilot, owner_client: TestClient):
        body = {"reasons": ["stale_state"], "explanation": "dated figure",
                "alternatives": {"stale_state": ["not_enough_info"]},
                "spans": [{"side": "state", "role": "support", "text": "1889", "start": Y0, "end": Y0 + 4,
                           "option": "true", "reasons": ["stale_state"]}]}
        r = owner_client.put("/api/admin/gold/fever/eval/5%23tall", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["reasons"] == ["stale_state"] and r.json()["alternatives"] == {"stale_state": ["not_enough_info"]}
        assert owner_client.get("/api/admin/gold").json()["count"] == 1
        r = owner_client.put("/api/admin/gold/fever/eval/5%23tall", json={"answerable": True})
        assert r.json()["answerable"] is True and r.json()["spans"] == []
        assert owner_client.post("/api/admin/gold/fever/eval/5%23tall/retire").json()["retired"] is True
        assert owner_client.get("/api/admin/gold").json()["count"] == 0
        assert owner_client.get("/api/admin/gold?include_retired=true").json()["count"] == 1

    def test_validation(self, pilot, owner_client: TestClient):
        url = "/api/admin/gold/fever/eval/5%23tall"
        assert owner_client.put(url, json={}).status_code == 422  # neither answerable nor reasons
        assert owner_client.put(url, json={"reasons": ["stale_state"]}).status_code == 422  # hard span rule
        bad = {"reasons": ["ambiguous"], "spans": [{"side": "state", "role": "support", "text": "nope",
                                                    "start": 0, "end": 4}]}
        assert "slice" in owner_client.put(url, json=bad).text
        assert owner_client.put("/api/admin/gold/nope%23q", json={"answerable": True}).status_code == 404

    def test_promote_from_annotation(self, labelled, owner_client: TestClient):
        r = owner_client.post("/api/admin/gold/fever/eval/5%23tall/promote", json={"labeler": "L01"})
        assert r.status_code == 200, r.text
        g = r.json()
        assert g["reasons"] == ["stale_state"] and [s["text"] for s in g["spans"]] == ["1889", "330"]
        r = owner_client.post("/api/admin/gold/fever/eval/5%23tall/promote", json={"labeler": "L09"})
        assert r.status_code == 422

    def test_public_admin_sees_only_libre_gold(self, pilot, fresh_client: TestClient):
        from e13_labeler.db import get_db
        from e13_labeler.gold import save_gold
        from tests.conftest import login

        with get_db() as conn:
            save_gold(conn, "bbc_news/eval/73#q0", answerable=True)
            save_gold(conn, FEVER, answerable=True)
        make_labeler("adm", role="admin", clearance="public")
        login(fresh_client, "adm")
        assert [g["item_id"] for g in fresh_client.get("/api/admin/gold").json()["gold"]] == [FEVER]
        assert fresh_client.put("/api/admin/gold/bbc_news/eval/73%23q0", json={"answerable": True}).status_code == 404

    def test_cli(self, labelled, tmp_path, capsys):
        from e13_labeler.__main__ import main

        assert main(["gold", "promote", FEVER, "--from", "L02", "--explanation", "x"]) == 0
        f = tmp_path / "gold.jsonl"
        f.write_text(json.dumps({"item_id": "bbc_news/eval/73#q0", "reasons": ["not_enough_info"]}) + "\n")
        assert main(["gold", "import", str(f)]) == 0
        capsys.readouterr()
        assert main(["gold", "list"]) == 0
        listing = capsys.readouterr().out
        assert FEVER in listing and "not_enough_info" in listing
        assert main(["gold", "retire", FEVER]) == 0


class TestAdminApi:
    def test_agreement_and_export_endpoints(self, labelled, owner_client: TestClient, tmp_path, monkeypatch):
        from e13_labeler import config

        rep = owner_client.get("/api/admin/agreement?n_boot=50").json()
        assert rep["inter_rater"]["n_items_pairable"] == 2
        monkeypatch.setattr(config, "OUTPUTS_DIR", tmp_path)
        r = owner_client.post("/api/admin/export", json={"kinds": ["annotations"]})
        assert r.status_code == 200 and Path(r.json()["directory"]).parent == tmp_path / "exports"
        assert owner_client.post("/api/admin/export", json={"kinds": ["nope"]}).status_code == 422
