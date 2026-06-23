"""Pillow 10+ compatibility for MoviePy 1.x (removed Image.ANTIALIAS)."""


def apply_pillow_moviepy_compat() -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    if hasattr(Image, "ANTIALIAS"):
        return
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        Image.ANTIALIAS = resampling.LANCZOS
    else:
        Image.ANTIALIAS = Image.LANCZOS
