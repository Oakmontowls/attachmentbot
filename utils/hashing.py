from PIL import Image
import imagehash
from io import BytesIO

def phash_bytes(image_bytes: bytes):
	img = Image.open(BytesIO(image_bytes))
	return imagehash.phash(img)
