"""S3 folder picker: listing, guessing, validation and per-document keys (no real AWS calls)."""
import sys
import types

import pytest

import s3_images


def test_guess_folder_from_filename():
    folders = ["PHYSIOLOGY", "PSYCHIATRY", "QUESTIONS", "RADIOLOGY", "SAT", "SPM", "STUDENTS", "SURGERY", "TESTING", "uploads"]
    assert s3_images.guess_folder("Psychiatry Grand Test 3.docx", folders) == "PSYCHIATRY"
    assert s3_images.guess_folder("PSM_mock_2.docx", folders) == "SPM"
    assert s3_images.guess_folder("surgery-mcqs.docx", folders) == "SURGERY"
    assert s3_images.guess_folder("physio revision.docx", folders) == "PHYSIOLOGY"
    assert s3_images.guess_folder("Test 3.docx", folders) is None


def test_list_folders_falls_back_without_credentials(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "example-bucket")
    s3_images._folder_cache.update(at=0, folders=None)
    fake = types.SimpleNamespace(client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no credentials")))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    folders, source, error = s3_images.list_folders(force=True)
    assert source == "default" and "PSYCHIATRY" in folders and "no credentials" in error


def test_list_folders_reads_bucket(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "hyderabad-drbhatia")
    pages = [{"CommonPrefixes": [{"Prefix": "uploads/"}, {"Prefix": "PSYCHIATRY/"}], "IsTruncated": True, "NextContinuationToken": "t"},
             {"CommonPrefixes": [{"Prefix": "SURGERY/"}, {"Prefix": "../evil/"}], "IsTruncated": False}]
    client = types.SimpleNamespace(list_objects_v2=lambda **kw: pages.pop(0))
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: client))
    folders, source, _ = s3_images.list_folders(force=True)
    assert source == "bucket" and folders == ["PSYCHIATRY", "SURGERY", "uploads"]


def test_upload_key_uses_folder_and_document(monkeypatch, tmp_path):
    from PIL import Image
    img = tmp_path / "a.png"; Image.new("RGB", (40, 40), "white").save(img)
    puts = []
    client = types.SimpleNamespace(put_object=lambda **kw: puts.append(kw["Key"]), head_object=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: client))
    monkeypatch.setenv("S3_BUCKET", "hyderabad-drbhatia")
    out = s3_images.upload_images([{"path": str(img), "question_number": 1, "section": "QUESTION", "sequence": 1}],
                                  "PSYCHIATRY", 200, document="Psychiatry GT 3.docx")
    assert puts == ["PSYCHIATRY/uploads/Psychiatry_GT_3/Q001_QUESTION_01.webp"]
    assert out[0]["url"].endswith("/PSYCHIATRY/uploads/Psychiatry_GT_3/Q001_QUESTION_01.webp")
    with pytest.raises(ValueError):
        s3_images.upload_images([], "../etc", 200)


def test_endpoint_requires_existing_folder(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    import importlib, auth, app as appmod
    importlib.reload(auth); importlib.reload(appmod)
    c = appmod.app.test_client()
    c.post("/login", data={"mobile": "9876543210", "password": "pw"})
    monkeypatch.setattr(s3_images, "list_folders", lambda force=False: (["PSYCHIATRY", "SURGERY"], "bucket", None))
    appmod._jobs["j1"] = {"questions": [], "candidates": [{"image_records": [{"path": __file__, "question_number": 1}]}], "filename": "x.docx", "output_path": "/tmp/x.json"}
    assert c.get("/api/s3-folders?filename=Surgery%20test.docx").json["suggested"] == "SURGERY"
    assert "Choose the S3 folder" in c.post("/api/process-s3/j1", data={"target_kb": "200"}).json["error"]
    assert "not in the bucket" in c.post("/api/process-s3/j1", data={"target_kb": "200", "folder": "HACK"}).json["error"]
