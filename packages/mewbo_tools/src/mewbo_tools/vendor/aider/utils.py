"""Minimal Aider utilities used by vendored UI components."""

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".pdf"}


def is_image_file(file_name: str) -> bool:
    """Return True when filename has a known image extension."""
    file_name = str(file_name)
    return any(file_name.endswith(ext) for ext in IMAGE_EXTENSIONS)
