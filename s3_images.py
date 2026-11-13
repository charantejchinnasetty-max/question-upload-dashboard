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


def upload_images(image_records, subject="QUESTIONS", target_kb=200):
    """Upload compressed records and verify every object with head_object."""
    import boto3
    bucket, region, prefix, base = _settings()
    client = boto3.client("s3", region_name=region)
    safe_subject = re.sub(r"[^A-Za-z0-9_-]+", "_", subject or "QUESTIONS").strip("_") or "QUESTIONS"
    results = []
    for record in image_records:
        source = Path(record["path"])
        data, extension, content_type, under_target = _compressed(source, int(target_kb))
        sequence = int(record.get("sequence", len(results) + 1))
        section = str(record.get("section", "QUESTION")).upper()
        option = str(record.get("option_label") or "").upper()
        suffix = f"_{option}" if section == "OPTION" and option else ""
        key = f"{safe_subject}/{prefix}/Q{int(record['question_number']):03d}_{section}{suffix}_{sequence:02d}{extension}"
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type, CacheControl="public,max-age=31536000,immutable")
        client.head_object(Bucket=bucket, Key=key)
        results.append({**record, "key": key, "url": f"{base}/{key}", "bytes": len(data), "under_target": under_target, "status": "uploaded"})
    return results
