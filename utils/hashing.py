from PIL import Image
import imagehash
from io import BytesIO

from utils.image_loading import ignore_truncated_tiff_metadata_warning


def phash_bytes(image_bytes: bytes):
	with ignore_truncated_tiff_metadata_warning():
		with Image.open(BytesIO(image_bytes)) as img:
			return imagehash.phash(img)
