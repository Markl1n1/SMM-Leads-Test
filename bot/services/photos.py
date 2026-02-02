import uuid

from bot.config import ENABLE_LEAD_PHOTOS, SUPABASE_LEADS_BUCKET
from bot.logging import logger
from bot.services.supabase_client import get_supabase_storage_client


def build_lead_photo_path(lead_id: int, extension: str = "jpg") -> str:
    unique = uuid.uuid4().hex[:8]
    return f"photos/lead_{lead_id}_{unique}.{extension}"


async def download_photo_from_supabase(photo_url: str) -> bytes | None:
    try:
        if not photo_url or not photo_url.strip():
            logger.error("[PHOTO] Empty photo_url provided")
            return None
        photo_url = photo_url.split('?')[0]
        if '/object/public/' not in photo_url:
            logger.error(f"[PHOTO] Invalid Supabase Storage URL format: {photo_url}")
            return None
        parts = photo_url.split('/object/public/')
        if len(parts) < 2:
            logger.error(f"[PHOTO] Could not parse URL: {photo_url}")
            return None
        path_with_bucket = parts[1]
        path_parts = path_with_bucket.split('/', 1)
        if len(path_parts) < 2:
            logger.error(f"[PHOTO] Could not extract path from URL: {photo_url}")
            return None
        bucket_name = path_parts[0]
        storage_path = path_parts[1]
        logger.info(f"[PHOTO] Extracted bucket='{bucket_name}', path='{storage_path}' from URL")
        client = get_supabase_storage_client()
        if not client:
            logger.error("[PHOTO] Supabase Storage client is None, cannot download photo")
            return None
        file_data = client.storage.from_(bucket_name).download(storage_path)
        if file_data:
            logger.info(f"[PHOTO] Successfully downloaded photo from Supabase Storage: {len(file_data)} bytes")
            return file_data
        logger.error(f"[PHOTO] download() returned None for path: {storage_path}")
        return None
    except Exception as e:
        logger.error(f"[PHOTO] Error downloading photo from Supabase Storage: {e}", exc_info=True)
        return None


async def upload_lead_photo_to_supabase(bot, file_id: str, lead_id: int) -> str | None:
    if not ENABLE_LEAD_PHOTOS:
        logger.info(f"[PHOTO] Photo upload disabled by ENABLE_LEAD_PHOTOS for lead {lead_id}")
        return None
    client = get_supabase_storage_client()
    if not client:
        logger.error(f"[PHOTO] Supabase Storage client is None, cannot upload photo for lead {lead_id}")
        return None
    try:
        tg_file = await bot.get_file(file_id)
        logger.info(f"[PHOTO] Got Telegram file for lead {lead_id}: {tg_file.file_path if tg_file.file_path else 'no path'}")
        ext = "jpg"
        if tg_file.file_path:
            file_path_lower = tg_file.file_path.lower()
            if file_path_lower.endswith(".png"):
                ext = "png"
            elif file_path_lower.endswith(".webp"):
                ext = "webp"
            elif file_path_lower.endswith(".jpeg") or file_path_lower.endswith(".jpg"):
                ext = "jpg"
            elif file_path_lower.endswith(".gif"):
                ext = "gif"
            elif file_path_lower.endswith(".bmp"):
                ext = "bmp"
        file_bytes = await tg_file.download_as_bytearray()
        file_bytes = bytes(file_bytes)
        file_size = len(file_bytes)
        logger.info(f"[PHOTO] Downloaded photo for lead {lead_id}: {file_size} bytes, extension: {ext}")
        storage_path = build_lead_photo_path(lead_id, ext)
        logger.info(f"[PHOTO] Uploading photo to Supabase bucket '{SUPABASE_LEADS_BUCKET}' with path '{storage_path}'")
        content_type = "image/jpeg" if ext == "jpg" else f"image/{ext}"
        upload_response = client.storage.from_(SUPABASE_LEADS_BUCKET).upload(
            storage_path,
            file_bytes,
            {"content-type": content_type}
        )
        if upload_response:
            public_url = client.storage.from_(SUPABASE_LEADS_BUCKET).get_public_url(storage_path)
            logger.info(f"[PHOTO] Photo uploaded successfully for lead {lead_id}: {public_url}")
            return public_url
        logger.error(f"[PHOTO] Upload failed for lead {lead_id}: response is None")
        return None
    except Exception as e:
        logger.error(f"[PHOTO] Error uploading photo for lead {lead_id}: {e}", exc_info=True)
        return None

