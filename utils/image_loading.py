import warnings
from contextlib import contextmanager


@contextmanager
def ignore_truncated_tiff_metadata_warning():
	with warnings.catch_warnings():
		warnings.filterwarnings(
			"ignore",
			message=r"^Truncated File Read$",
			category=UserWarning,
			module=r"^PIL\.TiffImagePlugin$",
		)
		yield
