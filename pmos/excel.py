"""Container integrity checks for downloaded files; contents are never read.

``check_xlsx`` verifies the OOXML zip parts and CRCs. ``check_xls`` verifies a
legacy CFB/BIFF8 workbook's record boundaries (ported from the Shanxi
collector, where the site's native exports are BIFF8). ``check_document``
identifies a disclosure attachment by its bytes (PDF, Office, image, zip),
never by its file name: the site lists BIFF workbooks named ``.xlsx``.
"""
from __future__ import annotations

import io
import struct
import zipfile
from pathlib import PurePosixPath
import re

XLS_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")


class ContainerError(ValueError):
    pass


def zip_preflight(archive: zipfile.ZipFile) -> None:
    """Inspect directory metadata before any CRC decompression or XML read."""
    infos = archive.infolist()
    if len(infos) > 10000 or sum(info.file_size for info in infos) > 64 * 1024 * 1024:
        raise ContainerError("zip_size_limit")
    if len({info.filename for info in infos}) != len(infos):
        raise ContainerError("zip_duplicate_parts")
    for info in infos:
        name = info.filename
        if (not name or name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name)
                or ".." in PurePosixPath(name).parts):
            raise ContainerError("zip_part_path_invalid")
        if info.flag_bits & 1:
            raise ContainerError("zip_encrypted_unsupported")


def check_xlsx(payload: bytes) -> dict:
    if len(payload) > 64 * 1024 * 1024:
        raise ContainerError("zip_size_limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as book:
            zip_preflight(book)
            names = set(book.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise ContainerError("xlsx_parts_missing")
            if book.testzip() is not None:
                raise ContainerError("xlsx_crc_failed")
    except ContainerError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise ContainerError("xlsx_container_invalid") from None
    return {"format": ".xlsx", "parts": len(names)}


def check_xls(payload: bytes) -> dict:
    if not 512 <= len(payload) <= 64 * 1024 * 1024 or payload[:8] != XLS_SIGNATURE:
        raise ContainerError("xls_container_invalid")
    if payload[28:30] != b"\xfe\xff" or struct.unpack_from("<H", payload, 26)[0] not in {3, 4}:
        raise ContainerError("xls_container_invalid")
    shift = struct.unpack_from("<H", payload, 30)[0]
    if shift not in {9, 12} or len(payload) % (1 << shift):
        raise ContainerError("xls_container_damaged")
    try:
        import olefile
    except ImportError:
        raise ContainerError("xls_validator_unavailable") from None
    try:
        with olefile.OleFileIO(io.BytesIO(payload), raise_defects=olefile.DEFECT_INCORRECT) as container:
            paths = container.listdir(streams=True, storages=False)
            if any(p[-1] in {"EncryptedPackage", "EncryptionInfo"} for p in paths):
                raise ContainerError("xls_encrypted_unsupported")
            books = [p for p in paths if p == ["Workbook"]]
            if len(books) != 1:
                raise ContainerError("xls_workbook_stream_missing")
            size = container.get_size(books[0])
            data = container.openstream(books[0]).read()
            if not 24 <= size <= len(payload) or len(data) != size:
                raise ContainerError("xls_stream_damaged")
    except ContainerError:
        raise
    except Exception:
        raise ContainerError("xls_container_damaged") from None
    return {"format": ".xls", **_walk_biff(data)}


def _walk_biff(data: bytes) -> dict:
    offset = records = worksheets = 0
    active = None
    globals_done = flags = False
    declared: list[int] = []
    observed: list[int] = []
    while offset < len(data):
        if active is None and globals_done and worksheets and len(data) - offset <= 4096 and not data[offset:].strip(b"\0"):
            break
        if offset + 4 > len(data):
            raise ContainerError("xls_record_truncated")
        start = offset
        kind, size = struct.unpack_from("<HH", data, offset)
        offset += 4
        records += 1
        if size > 8224 or offset + size > len(data) or records > 250000:
            raise ContainerError("xls_record_truncated")
        if kind == 0x0809:  # BOF
            if active is not None or size != 16:
                raise ContainerError("xls_substream_invalid")
            version, document = struct.unpack_from("<HH", data, offset)
            if version != 0x0600:
                raise ContainerError("xls_biff_version_unsupported")
            if document == 0x0005 and records == 1:
                active = "globals"
            elif document == 0x0010 and globals_done:
                worksheets += 1
                active, flags = "worksheet", False
                observed.append(start)
            else:
                raise ContainerError("xls_substream_unsupported")
        elif kind == 0x000A:  # EOF
            if size or active is None:
                raise ContainerError("xls_substream_invalid")
            if active == "globals":
                globals_done = True
            elif not flags:
                raise ContainerError("xls_worksheet_flags_missing")
            active = None
        elif active is None:
            raise ContainerError("xls_substream_invalid")
        elif kind == 0x002F:
            raise ContainerError("xls_encrypted_unsupported")
        elif kind == 0x0085 and active == "globals":  # BOUNDSHEET
            if size < 9 or data[offset + 5] != 0:
                raise ContainerError("xls_substream_unsupported")
            declared.append(struct.unpack_from("<I", data, offset)[0])
        elif kind == 0x0081 and active == "worksheet":  # WSBOOL
            if size != 2:
                raise ContainerError("xls_worksheet_flags_invalid")
            flags = True
        offset += size
    if active is not None or not globals_done or not worksheets:
        raise ContainerError("xls_substream_incomplete")
    if sorted(declared) != sorted(observed) or len(set(declared)) != len(declared):
        raise ContainerError("xls_sheet_directory_mismatch")
    return {"worksheets": worksheets, "records": records}


def check_document(payload: bytes) -> dict:
    """Format of a disclosure attachment from its signature, with a container check."""
    if not payload:
        raise ContainerError("empty_file")
    if payload.startswith(b"%PDF-"):
        if b"%%EOF" not in payload[-4096:]:
            raise ContainerError("pdf_container_invalid")
        return {"format": ".pdf"}
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        if b"IEND" not in payload[-64:]:
            raise ContainerError("png_truncated")
        return {"format": ".png"}
    if payload.startswith(b"\xff\xd8\xff"):
        if b"\xff\xd9" not in payload[-64:]:
            raise ContainerError("jpeg_truncated")
        return {"format": ".jpg"}
    if payload.startswith(b"PK\x03\x04"):
        if len(payload) > 64 * 1024 * 1024:
            raise ContainerError("zip_size_limit")
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                zip_preflight(archive)
                names = set(archive.namelist())
                if archive.testzip() is not None:
                    raise ContainerError("zip_crc_failed")
        except ContainerError:
            raise
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
            raise ContainerError("zip_container_invalid") from None
        if "xl/workbook.xml" in names:
            return {"format": ".xlsx", "parts": len(names)}
        if "word/document.xml" in names:
            return {"format": ".docx", "parts": len(names)}
        return {"format": ".zip", "parts": len(names)}
    if payload.startswith(XLS_SIGNATURE):
        try:
            import olefile
        except ImportError:
            raise ContainerError("ole_validator_unavailable") from None
        try:
            with olefile.OleFileIO(io.BytesIO(payload)) as container:
                streams = {p[-1] for p in container.listdir(streams=True, storages=False)}
                header = container.openstream("WordDocument").read(2) if "WordDocument" in streams else b""
        except Exception:
            raise ContainerError("ole_container_damaged") from None
        if "Workbook" in streams:
            return check_xls(payload)
        if "WordDocument" in streams:
            # MS-DOC FibBase.wIdent is 0xA5EC.
            if header != b"\xec\xa5":
                raise ContainerError("doc_signature_invalid")
            return {"format": ".doc"}
        raise ContainerError("ole_document_kind_unknown")
    raise ContainerError("document_format_unreviewed")
