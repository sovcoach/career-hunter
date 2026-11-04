"""Reading a resume: PDF and DOCX, decided from the bytes, with fixed errors and caps.

Every file here is built in the test from made-up text.
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pypdf
import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from careerhunter import resume

_SAMPLE = "Avery Example. Operations coordinator at Example Widgets Ltd."
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)


def make_pdf(pages: list[str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    for text in pages:
        pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def document_xml(body: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}"><w:body>{body}'
        "</w:body></w:document>"
    ).encode()


def make_docx(
    xml: bytes,
    *,
    extra: int = 0,
    compression: int = zipfile.ZIP_DEFLATED,
    parts: tuple[str, ...] = ("[Content_Types].xml", "word/document.xml"),
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        if "[Content_Types].xml" in parts:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        if "word/document.xml" in parts:
            archive.writestr("word/document.xml", xml)
        for i in range(extra):
            archive.writestr(f"word/media/part{i}.xml", b"<x/>")
    return buffer.getvalue()


def _central_entry(data: bytes, name: bytes) -> int:
    """Offset of the central-directory header for `name`."""
    offset = 0
    while True:
        offset = data.index(b"PK\x01\x02", offset)
        name_length = struct.unpack_from("<H", data, offset + 28)[0]
        if data[offset + 46 : offset + 46 + name_length] == name:
            return offset
        offset += 4


def _patched(data: bytes, field: int, fmt: str, value: int) -> bytes:
    """Rewrite one field of document.xml's central-directory header."""
    out = bytearray(data)
    struct.pack_into(fmt, out, _central_entry(data, b"word/document.xml") + field, value)
    return bytes(out)


def _refused(data: bytes, reason: str) -> resume.ResumeError:
    with pytest.raises(resume.ResumeError) as caught:
        resume.extract_text(data)
    err = caught.value
    assert err.reason == reason
    assert str(err) == resume._MESSAGES[reason]
    assert err.__cause__ is None
    assert err.__suppress_context__ or err.__context__ is None
    return err


# --- format sniffing ----------------------------------------------------------------------


def test_pdf_text_is_read() -> None:
    assert resume.extract_text(make_pdf([_SAMPLE])) == _SAMPLE


def test_docx_text_is_read_with_tabs_breaks_and_paragraphs() -> None:
    body = (
        "<w:p><w:r><w:t>Avery Example</w:t><w:tab/><w:t>Coordinator</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Line one</w:t><w:br/><w:t>Line two</w:t></w:r></w:p>"
    )
    text = resume.extract_text(make_docx(document_xml(body)))
    assert text == "Avery Example\tCoordinator\nLine one\nLine two"


@pytest.mark.parametrize(
    "data",
    [
        b"plain text that is not a resume format",
        b"%PDF without the dash",
        b"",
        b"\x89PNG\r\n\x1a\n",
    ],
)
def test_other_bytes_are_unsupported(data: bytes) -> None:
    _refused(data, "unsupported")


@pytest.mark.parametrize("parts", [("[Content_Types].xml",), ("word/document.xml",), ()])
def test_a_zip_without_both_docx_parts_is_unsupported(parts: tuple[str, ...]) -> None:
    _refused(make_docx(document_xml(""), parts=parts), "unsupported")


def test_the_file_name_does_not_decide_the_format(tmp_path: Path) -> None:
    path = tmp_path / "resume.pdf"
    path.write_bytes(make_docx(document_xml("<w:p><w:r><w:t>Docx inside</w:t></w:r></w:p>")))
    assert resume.extract_text(resume.read_file(path)) == "Docx inside"


# --- PDF ------------------------------------------------------------------------------------


def test_an_encrypted_pdf_is_refused_before_any_page_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(make_pdf([_SAMPLE]))))
    writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    touched: list[str] = []
    monkeypatch.setattr(pypdf.PdfReader, "pages", property(lambda _self: touched.append("pages")))
    _refused(buffer.getvalue(), "encrypted")
    assert touched == []


def test_a_broken_pdf_is_refused() -> None:
    _refused(b"%PDF-1.4\nnot really a pdf at all", "pdf_unreadable")


def test_too_many_pages_are_refused_before_any_text_is_extracted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resume, "MAX_PAGES", 2)
    calls: list[int] = []
    original = pypdf.PageObject.extract_text

    def spy(self: pypdf.PageObject, *args: object, **kwargs: object) -> str:
        calls.append(1)
        return original(self)

    monkeypatch.setattr(pypdf.PageObject, "extract_text", spy)
    _refused(make_pdf(["one", "two", "three"]), "too_many_pages")
    assert calls == []


def test_pdf_text_cap_is_checked_page_by_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def long_page(self: pypdf.PageObject, *args: object, **kwargs: object) -> str:
        calls.append(1)
        return "x" * 30_000

    monkeypatch.setattr(pypdf.PageObject, "extract_text", long_page)
    _refused(make_pdf(["a", "b", "c", "d"]), "too_long")
    assert calls == [1, 1]  # refused at the second page; the third is never read


def test_pdf_text_exactly_at_the_cap_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pypdf.PageObject, "extract_text", lambda self, *a, **k: "y" * (resume.MAX_CHARS - 1)
    )
    assert len(resume.extract_text(make_pdf(["only"]))) == resume.MAX_CHARS - 1


def test_a_pdf_with_no_text_is_refused() -> None:
    _refused(make_pdf([""]), "empty")


def test_pypdf_log_records_do_not_escape(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    with pytest.raises(resume.ResumeError):
        resume.extract_text(b"%PDF-1.4\n" + b"Sentinel-Lorem-Ipsum " * 20)
    assert not [r for r in caplog.records if r.name.startswith("pypdf")]


# --- DOCX -------------------------------------------------------------------------------------


def test_too_many_zip_entries_are_refused() -> None:
    _refused(make_docx(document_xml(""), extra=resume.MAX_ZIP_ENTRIES - 1), "docx_entries")


def test_exactly_the_entry_cap_is_kept() -> None:
    data = make_docx(
        document_xml("<w:p><w:r><w:t>kept</w:t></w:r></w:p>"), extra=resume.MAX_ZIP_ENTRIES - 2
    )
    assert resume.extract_text(data) == "kept"


def test_document_xml_over_the_cap_is_refused_by_a_bounded_read() -> None:
    filler = "<w:p><w:r><w:t>" + "z" * 1000 + "</w:t></w:r></w:p>"
    xml = document_xml(filler * (resume.MAX_DOCUMENT_XML // 1000 + 10))
    assert len(xml) > resume.MAX_DOCUMENT_XML
    _refused(make_docx(xml), "docx_xml_large")


def test_a_size_header_that_understates_document_xml_is_never_trusted() -> None:
    """The header claims 100 bytes. The read stops there and the CRC check refuses it."""
    filler = "<w:p><w:r><w:t>" + "q" * 1000 + "</w:t></w:r></w:p>"
    lying = _patched(make_docx(document_xml(filler * 50)), 24, "<I", 100)
    _refused(lying, "docx_zip")


def test_bad_zip_maps_to_a_fixed_message() -> None:
    _refused(b"PK\x03\x04" + b"\x00" * 64, "docx_zip")


def test_unsupported_compression_maps_to_a_fixed_message() -> None:
    data = _patched(make_docx(document_xml("<w:p/>")), 10, "<H", 99)
    _refused(data, "docx_compression")


def test_xml_parse_error_maps_to_a_fixed_message() -> None:
    _refused(make_docx(b"<w:document><w:body><w:p></w:body>"), "docx_xml")


@pytest.mark.parametrize("doctype", ["<!DOCTYPE w:document>", "<!doctype x [<!ENTITY a 'b'>]>"])
def test_a_doctype_is_refused(doctype: str) -> None:
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>{doctype}<w:document xmlns:w="{_W}"/>').encode()
    _refused(make_docx(xml), "docx_doctype")


@pytest.mark.parametrize(
    "where",
    [
        "start",
        "after a long comment",
        "inside the body",
        "at the very end",
    ],
)
def test_a_doctype_is_found_anywhere_in_document_xml(where: str) -> None:
    doctype = "<!DocType w:document>"
    body = "<w:p><w:r><w:t>Example text</w:t></w:r></w:p>"
    if where == "start":
        text = doctype + f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    elif where == "after a long comment":
        text = (
            '<?xml version="1.0"?><!--'
            + " filler" * 2000
            + " -->"
            + doctype
            + f'<w:document xmlns:w="{_W}"/>'
        )
    elif where == "inside the body":
        text = f'<w:document xmlns:w="{_W}"><w:body>{body}{doctype}</w:body></w:document>'
    else:
        text = f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>' + doctype
    _refused(make_docx(text.encode()), "docx_doctype")


_UTF16_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "boom">]>'
    f'<w:document xmlns:w="{_W}"><w:body><w:p><w:r><w:t>&e;</w:t></w:r></w:p></w:body>'
    "</w:document>"
)


@pytest.mark.parametrize(
    "codec",
    ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"],
)
def test_utf16_and_utf32_are_refused_before_parsing(
    codec: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With or without a byte-order mark. Without one, the bytes are valid UTF-8."""
    parsed: list[bytes] = []
    monkeypatch.setattr(
        resume.ET, "iterparse", lambda source, *a, **k: parsed.append(source.getvalue()) or []
    )
    _refused(make_docx(_UTF16_DOCTYPE.encode(codec)), "docx_encoding")
    assert parsed == []


def test_the_parser_gets_exactly_the_checked_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = b"\xef\xbb\xbf" + document_xml("<w:p><w:r><w:t>café</w:t></w:r></w:p>")
    checked: list[bytes] = []
    parsed: list[bytes] = []
    real_check, real_parse = resume._checked_xml, resume.ET.iterparse

    def check(data: bytes) -> bytes:
        checked.append(data)
        return real_check(data)

    def parse(source: io.BytesIO, *args: object, **kwargs: object) -> object:
        parsed.append(source.getvalue())
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(resume, "_checked_xml", check)
    monkeypatch.setattr(resume.ET, "iterparse", parse)
    assert resume.extract_text(make_docx(xml)) == "café"
    assert checked == parsed == [xml]


def test_a_declared_encoding_other_than_utf8_is_refused() -> None:
    xml = b'<?xml version="1.0" encoding="ISO-8859-1"?><w:document/>'
    _refused(make_docx(xml), "docx_encoding")


def test_docx_text_cap_is_checked_while_collecting() -> None:
    body = "<w:p><w:r><w:t>" + "w" * (resume.MAX_CHARS + 1) + "</w:t></w:r></w:p>"
    _refused(make_docx(document_xml(body)), "too_long")


def test_a_docx_with_no_text_is_refused() -> None:
    _refused(make_docx(document_xml("<w:p/>")), "empty")


# --- reading the file -------------------------------------------------------------------------


def test_a_file_over_the_size_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resume, "MAX_FILE_BYTES", 1024)
    path = tmp_path / "big.pdf"
    path.write_bytes(b"%PDF-" + b"0" * 1024)
    with pytest.raises(resume.ResumeError) as caught:
        resume.read_file(path)
    assert caught.value.reason == "too_large"


def test_a_missing_file_or_a_folder_is_a_fixed_error(tmp_path: Path) -> None:
    for path in (tmp_path / "missing.pdf", tmp_path):
        with pytest.raises(resume.ResumeError) as caught:
            resume.read_file(path)
        assert caught.value.reason == "open"
        assert str(tmp_path) not in str(caught.value)
        assert caught.value.__cause__ is None


def test_messages_carry_no_content_or_path() -> None:
    for reason, message in resume._MESSAGES.items():
        assert str(resume.ResumeError(reason)) == message
        assert "/" not in message and "Avery" not in message
    with pytest.raises(ValueError, match="unknown resume error reason"):
        resume.ResumeError("made up")
