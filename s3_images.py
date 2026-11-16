"""Server-side DOCX image compression and S3 upload helpers."""
import io
import os
import re
from pathlib import Path

from PIL import Image


def _settings():
    bucket = os.environ.get("S3_BUCKET") or os.environ.get("AWS_S3_BUCKET")
    region = os.environ.get("S3_REGION") or os.environ.get("AWS_REGION") or "ap-south-1"
    prefix = (os.environ.get("S3_PREFIX") or "uploads").strip("/")
    base = os.environ.get("S3_PUBLIC_BASE_URL") or (f"https://{bucket}.s3.{region}.amazonaws.com" if bucket else "")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not configured. Add S3_BUCKET, S3_REGION and server-side AWS credentials before processing images.")
    return bucket, region, prefix, base.rstrip("/")


def _compressed(path, target_kb=200):
    image = Image.open(path)
    image.load()
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    output = io.BytesIO()
    if has_alpha:
        image = image.convert("RGBA")
        extension, content_type = ".png", "image/png"
        image.save(output, format="PNG", optimize=True)
    else:
        image = image.convert("RGB")
        extension, content_type = ".webp", "image/webp"
        quality = 86
        image.save(output, format="WEBP", quality=quality, method=6)
        while output.tell() > target_kb * 1024 and quality > 45:
            quality -= 8; output = io.BytesIO(); image.save(output, format="WEBP", quality=quality, method=6)
    return output.getvalue(), extension, content_type, output.tell() <= target_kb * 1024


# Used when the AWS user may not list the bucket: the subject folders seen in the hyderabad-drbhatia bucket
# plus the standard NEET PG subjects.
DEFAULT_FOLDERS = [
    "ANATOMY", "PHYSIOLOGY", "BIOCHEMISTRY", "PATHOLOGY", "PHARMACOLOGY", "MICROBIOLOGY", "FMT", "SPM", "ENT",
    "OPHTHALMOLOGY", "MEDICINE", "SURGERY", "OBG", "PEDIATRICS", "ORTHOPEDICS", "DERMATOLOGY", "PSYCHIATRY",
    "ANAESTHESIA", "RADIOLOGY", "QUESTIONS", "SAT", "TESTING",
]
_folder_cache = {"at": 0.0, "folders": None}
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _&().-]{0,79}$")


def list_folders(force=False):
    """Top-level folders in the bucket (cached 5 minutes). Returns (folders, source, error)."""
    import time
    if not force and _folder_cache["folders"] is not None and time.time() - _folder_cache["at"] < 300:
        return _folder_cache["folders"], "bucket", None
    try:
        import boto3
        bucket, region, _, _ = _settings()
        client = boto3.client("s3", region_name=region)
        folders, token = [], None
        while True:
            kw = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = client.list_objects_v2(**kw)
            folders += [p["Prefix"].rstrip("/") for p in page.get("CommonPrefixes", [])]
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        folders = sorted({f for f in folders if f and FOLDER_RE.match(f)}, key=lambda f: f.lower())
        _folder_cache.update(at=time.time(), folders=folders)
        return folders, "bucket", None
    except Exception as exc:  # no credentials / no ListBucket permission
        return DEFAULT_FOLDERS, "default", str(exc)[:200]


def guess_folder(filename, folders):
    """Pick the folder whose name appears in the document name, e.g. 'Psychiatry GT 3.docx' -> PSYCHIATRY."""
    name = re.sub(r"[^a-z0-9]+", " ", (filename or "").lower())
    aliases = {"psm": "SPM", "community medicine": "SPM", "forensic": "FMT", "gynae": "OBG", "obstetrics": "OBG",
               "paediatrics": "PEDIATRICS", "pediatric": "PEDIATRICS", "ortho": "ORTHOPEDICS", "ophthal": "OPHTHALMOLOGY",
               "derma": "DERMATOLOGY", "anesthesia": "ANAESTHESIA", "micro": "MICROBIOLOGY", "pharma": "PHARMACOLOGY",
               "patho": "PATHOLOGY", "physio": "PHYSIOLOGY", "biochem": "BIOCHEMISTRY", "radio": "RADIOLOGY", "psych": "PSYCHIATRY"}
    by_lower = {f.lower(): f for f in folders}
    for f in sorted(folders, key=len, reverse=True):
        if len(f) >= 3 and re.search(r"\b" + re.escape(f.lower()) + r"\b", name):
            return f
    for key, target in aliases.items():
        if re.search(r"\b" + re.escape(key), name) and target.lower() in by_lower:
            return by_lower[target.lower()]
    return None


def document_slug(filename):
    stem = Path(filename or "document").stem
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:60] or "document"


def upload_images(image_records, subject="QUESTIONS", target_kb=200, document=None):
    """Upload compressed records and verify every object with head_object.
    Key: <folder>/<S3_PREFIX>/<document>/Q001_QUESTION_01.webp — one subfolder per document so uploads
    of different exams never overwrite each other."""
    import boto3
    bucket, region, prefix, base = _settings()
    client = boto3.client("s3", region_name=region)
    if not FOLDER_RE.match(subject or ""):
        raise ValueError("Choose a valid S3 folder.")
    safe_subject = subject.strip()
    doc_part = f"{document_slug(document)}/" if document else ""
    results = []
    for record in image_records:
        source = Path(record["path"])
        data, extension, content_type, under_target = _compressed(source, int(target_kb))
        sequence = int(record.get("sequence", len(results) + 1))
        section = str(record.get("section", "QUESTION")).upper()
        option = str(record.get("option_label") or "").upper()
        suffix = f"_{option}" if section == "OPTION" and option else ""
        key = f"{safe_subject}/{prefix}/{doc_part}Q{int(record['question_number']):03d}_{section}{suffix}_{sequence:02d}{extension}"
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type, CacheControl="public,max-age=31536000,immutable")
        client.head_object(Bucket=bucket, Key=key)
        results.append({**record, "key": key, "url": f"{base}/{key}", "bytes": len(data), "under_target": under_target, "status": "uploaded"})
    return results
