"""Sandboxed loading of the images that label elements draw.

This module exists because of one verified defect in the previous
implementation: an image reference is *record data*, i.e. user input, and it
reached the filesystem unsandboxed. ``{"logo": "/etc/passwd"}`` and
``{"logo": "../../../etc/hostname"}`` were both read from disk and embedded
into the returned PDF. reportlab's ``ImageReader`` will also fetch a URL if
handed a string that looks like one, so the same field was an outbound request
primitive.

Three structural changes close that off, in preference to blacklisting:

* Images are **off** unless an operator configures an asset directory. A public
  instance with no ``LSG_ASSET_DIR`` cannot read any file at all through here.
* A reference is an *identifier plus an extension*, never a path. It is
  resolved only by :func:`label_sheet_generator.fsio.resolve_in_root`, which
  confirms containment after symlink resolution.
* ``ImageReader`` is handed a :class:`io.BytesIO` over bytes this module
  already read and inspected. It never sees a name, so its path and URL
  fallbacks are unreachable rather than merely unused.

Messages raised from here deliberately name only the reference the caller
supplied. Echoing the resolved path would turn a 404 into a file-existence
oracle for the whole container.
"""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from threading import Lock

import reportlab.rl_config
from PIL import Image
from reportlab.lib.utils import ImageReader

from label_sheet_generator.errors import (
    AssetError,
    ConfigurationError,
    LabelSheetError,
    NotFoundError,
    UnsafePathError,
)
from label_sheet_generator.fsio import read_bytes, resolve_in_root

# reportlab consults these when it is given something string-shaped and decides
# whether to open it as a URL. Upstream ships a permissive default (``http``,
# ``https`` and ``ftp`` are trusted, and ``trustedHosts`` of ``None`` means
# "any host"). Nothing here should ever depend on that default being tightened
# elsewhere, and a future reportlab release could widen it again, so both lists
# are pinned empty at import time. This module additionally never hands
# reportlab a string, so the pin is a second line of defence, not the only one.
reportlab.rl_config.trustedHosts = []
reportlab.rl_config.trustedSchemes = []

#: Extensions an image reference may carry. Restricted to the raster formats
#: reportlab can embed; SVG and PDF are excluded because they are documents
#: with their own external-reference semantics, not pixels.
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

#: The PIL format tags corresponding to :data:`ALLOWED_IMAGE_SUFFIXES`. Checked
#: against the sniffed header, not the name, so a ``.png`` that is really a
#: TIFF or an MPO is refused rather than handed to a decoder the allowlist was
#: never meant to reach.
ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}

#: Decoded readers retained by default. Sheets repeat the same handful of
#: images across hundreds of labels, so a small cache removes almost all
#: repeat decoding while staying bounded regardless of how many distinct
#: references a record set names.
DEFAULT_CACHE_SIZE = 32


def _split_reference(reference: str) -> tuple[str, str]:
    """Split ``"logo.png"`` into its identifier and its suffix.

    The split happens here rather than inside
    :func:`~label_sheet_generator.fsio.resolve_in_root` because that function's
    identifier grammar forbids dots outright: handing it ``"logo.png"`` whole
    would reject every legitimate filename. Taking only the final extension and
    leaving everything before it to be validated as an identifier means
    ``"../x.png"`` and ``"a.b.png"`` still fail, since ``".."`` and ``"a.b"``
    are not safe identifiers.

    Raises:
        AssetError: if the reference carries no usable extension.
    """
    stem, dot, extension = reference.rpartition(".")
    if not dot or not stem or not extension:
        raise AssetError(f"image reference {reference!r} must end in one of {_suffix_list()}")
    # The suffix is matched case-insensitively but preserved as written, so a
    # file genuinely named ``Logo.PNG`` still resolves on a case-sensitive
    # filesystem.
    suffix = "." + extension
    if suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise AssetError(
            f"image reference {reference!r} has an unsupported extension; "
            f"allowed extensions are {_suffix_list()}"
        )
    return stem, suffix


def _suffix_list() -> str:
    """The allowlist rendered for an error message, in a stable order."""
    return ", ".join(sorted(ALLOWED_IMAGE_SUFFIXES))


class AssetLoader:
    """Loads label images from one directory, or from nowhere at all.

    Args:
        root: The only directory images may come from. ``None`` disables image
            elements entirely, which is the default for a public instance.
        max_bytes: Refuse any file larger than this before reading it.
        max_pixels: Refuse any image whose pixel count exceeds this, checked
            from the header before a decode is attempted.
        cache_size: Maximum number of decoded readers retained. Zero disables
            caching.

    Raises:
        ConfigurationError: if a limit is not a positive integer.
    """

    def __init__(
        self,
        root: Path | None,
        *,
        max_bytes: int,
        max_pixels: int,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if max_bytes <= 0:
            raise ConfigurationError(f"max_bytes must be positive, got {max_bytes}")
        if max_pixels <= 0:
            raise ConfigurationError(f"max_pixels must be positive, got {max_pixels}")
        if cache_size < 0:
            raise ConfigurationError(f"cache_size cannot be negative, got {cache_size}")

        self._root = root
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._cache_size = cache_size
        # PIL's own bomb guard is a module global, so it can only be aligned
        # with this loader's limit globally. Setting it low is always the safe
        # direction: it makes PIL raise before we do on the extreme cases.
        Image.MAX_IMAGE_PIXELS = max_pixels
        self._cache: OrderedDict[str, ImageReader] = OrderedDict()
        # Renders can run concurrently, and OrderedDict reordering is not
        # atomic across the read-then-move-to-end pair below.
        self._lock = Lock()

    @property
    def enabled(self) -> bool:
        """True when an asset directory is configured and images may load."""
        return self._root is not None

    def clear(self) -> None:
        """Drop every cached reader."""
        with self._lock:
            self._cache.clear()

    def load(self, reference: str) -> ImageReader:
        """Resolve and decode ``reference`` into a reportlab image reader.

        Args:
            reference: A plain filename such as ``"logo.png"``, optionally
                namespaced with forward slashes (``"brand/logo.png"``). Never a
                path, an absolute location, or a URL.

        Raises:
            AssetError: if images are disabled, the extension is not allowed,
                or the bytes are not a decodable image of an allowed format.
            UnsafePathError: if the reference is not a contained identifier.
            NotFoundError: if no such image exists.
            LimitExceeded: if the file or its pixel count exceeds a limit.
        """
        candidate: object = reference
        if not isinstance(candidate, str) or not candidate.strip():
            raise AssetError("image reference must be a non-empty name")

        reference = candidate.strip()
        cached = self._cache_get(reference)
        if cached is not None:
            return cached

        reader = self._load_uncached(reference)
        self._cache_put(reference, reader)
        return reader

    # -- internals ---------------------------------------------------------

    def _load_uncached(self, reference: str) -> ImageReader:
        path = self._resolve(reference)
        what = f"image {reference!r}"
        data = read_bytes(path, max_bytes=self._max_bytes, what=what)
        self._check_header(reference, data)
        try:
            return ImageReader(BytesIO(data))
        except LabelSheetError:
            raise
        except Exception as exc:  # reportlab raises assorted untyped errors
            raise AssetError(f"{what} could not be read as an image") from exc

    def _resolve(self, reference: str) -> Path:
        """Map a reference to a contained path, without leaking that path."""
        if self._root is None:
            raise AssetError(
                f"image {reference!r} cannot be loaded because this server has no "
                "asset directory configured; image elements are disabled"
            )

        identifier, suffix = _split_reference(reference)
        try:
            return resolve_in_root(self._root, identifier, suffix=suffix, kind="image")
        except NotFoundError as exc:
            # Re-raised so the message names the reference the caller actually
            # wrote, extension included, instead of the bare identifier.
            raise NotFoundError(f"image {reference!r} was not found") from exc
        except UnsafePathError as exc:
            raise UnsafePathError(
                f"image reference {reference!r} is not a plain name inside the asset directory"
            ) from exc

    def _check_header(self, reference: str, data: bytes) -> None:
        """Reject decompression bombs and disallowed formats before decoding.

        ``Image.open`` parses only the header, so the dimensions are known
        while the pixel buffer still costs nothing. A 40,000 x 40,000 PNG is a
        few hundred kilobytes on disk and several gigabytes decoded, which is
        why the byte cap alone is not sufficient.
        """
        try:
            with Image.open(BytesIO(data)) as image:
                width, height, image_format = image.width, image.height, image.format
        except Image.DecompressionBombError as exc:
            # PIL trips its own guard at twice MAX_IMAGE_PIXELS, which is below
            # the point our explicit count check would be reached.
            raise AssetError(
                f"image {reference!r} exceeds the {self._max_pixels} pixel maximum"
            ) from exc
        except Exception as exc:  # PIL raises OSError, ValueError and its own types
            raise AssetError(
                f"image {reference!r} could not be decoded; it may be corrupt or "
                "not an image at all"
            ) from exc

        if image_format not in ALLOWED_IMAGE_FORMATS:
            raise AssetError(
                f"image {reference!r} contains {image_format or 'unknown'} data; "
                f"allowed formats are {', '.join(sorted(ALLOWED_IMAGE_FORMATS))}"
            )

        pixels = width * height
        if pixels > self._max_pixels:
            raise AssetError(
                f"image {reference!r} is {width}x{height} = {pixels} pixels; "
                f"the maximum is {self._max_pixels}"
            )

    def _cache_get(self, reference: str) -> ImageReader | None:
        if self._cache_size <= 0:
            return None
        with self._lock:
            reader = self._cache.get(reference)
            if reader is not None:
                self._cache.move_to_end(reference)
            return reader

    def _cache_put(self, reference: str, reader: ImageReader) -> None:
        if self._cache_size <= 0:
            return
        with self._lock:
            self._cache[reference] = reader
            self._cache.move_to_end(reference)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
