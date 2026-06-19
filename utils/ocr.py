import pytesseract
import time
from io import BytesIO
from PIL import Image
import numpy as np
from core.logger import log


def preprocess_image(image_bytes: bytes):
	img = Image.open(BytesIO(image_bytes)).convert("RGB")

	max_dimension = 1600
	if max(img.size) > max_dimension:
		img.thumbnail((max_dimension, max_dimension))

	return np.array(img)


def run_ocr(image_bytes: bytes):
	start = time.time()

	img = preprocess_image(image_bytes)

	text = pytesseract.image_to_string(img, config="--psm 6").lower()
	
	#log.info(text)

	log.info(f"OCR time: {time.time() - start:.2f}s")

	return text