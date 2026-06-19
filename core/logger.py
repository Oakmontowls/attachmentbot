import logging
import sys
from logging.handlers import RotatingFileHandler

LOG_FILE = "bot_debug.log"

def setup_logger():
	logger = logging.getLogger("attachmentbot")

	if logger.handlers:
		return logger

	logger.setLevel(logging.INFO)

	fmt = logging.Formatter(
		"[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
		"%Y-%m-%d %H:%M:%S"
	)

	file_handler = RotatingFileHandler(
		LOG_FILE,
		maxBytes=5_000_000,
		backupCount=3,
		encoding="utf-8"
	)

	console_handler = logging.StreamHandler(sys.stdout)

	file_handler.setFormatter(fmt)
	console_handler.setFormatter(fmt)

	logger.addHandler(file_handler)
	logger.addHandler(console_handler)

	logger.propagate = False

	return logger


log = setup_logger()