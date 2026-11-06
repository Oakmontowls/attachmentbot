import argparse
from pathlib import Path

from utils.ocr import run_ocr


def main():
	parser = argparse.ArgumentParser(
		description="Run an image through AttachmentBot's OCR pipeline.",
	)
	parser.add_argument("image", type=Path, help="Path to the image file")
	args = parser.parse_args()

	image_bytes = args.image.read_bytes()
	text = run_ocr(image_bytes)
	print(text, end="" if text.endswith("\n") else "\n")


if __name__ == "__main__":
	main()
