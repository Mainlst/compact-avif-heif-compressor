"""Image Compressor's reusable local conversion API."""

__all__ = [
    "INPUT_EXTENSIONS",
    "CompressionResult",
    "CompressionSettings",
    "OutputFormat",
    "available_formats",
    "collect_images",
    "convert_image",
]


def __getattr__(name: str):
    # Window setup and the package itself do not need native image libraries.
    # Keep the public conversion API unchanged, loading it on first use.
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(".engine", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
