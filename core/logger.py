import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parents[1] / "bot_debug.log"

def setup_logger():
	logger = logging.getLogger("attachmentbot")

	if logger.handlers:
		return logger

	logger.setLevel(logging.INFO)

	fmt = logging.Formatter(
		"[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
		"%Y-%m-%d %H:%M:%S"
	)

	console_handler = logging.StreamHandler(sys.stdout)
	console_handler.setFormatter(fmt)

	logger.addHandler(console_handler)

	try:
		LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
		LOG_FILE.touch(exist_ok=True)
		file_handler = RotatingFileHandler(
			LOG_FILE,
			maxBytes=5_000_000,
			backupCount=3,
			encoding="utf-8"
		)
		file_handler.setFormatter(fmt)
		logger.addHandler(file_handler)
	except OSError as exc:
		print(f"AttachmentBot could not open log file {LOG_FILE}: {exc}", file=sys.stderr)

	logger.propagate = False

	return logger


log = setup_logger()
