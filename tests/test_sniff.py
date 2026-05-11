"""Tests for :mod:`litspectraits.sniff` (``docs/overview-v3.md`` §17.4).

The fixtures are inline byte literals: tiny, hermetic, and explicit
about which adversarial case they exercise. Real-publisher samples
belong in the gated end-to-end smoke tests (§22), not here.
"""

from pathlib import Path

import pytest

from litspectraits.errors import MalformedArtifactError
from litspectraits.manifest import Format
from litspectraits.sniff import classify, verify

# Fixture bytes ---------------------------------------------------------------

_PDF_HEAD = b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n'

_JATS_BARE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<article xmlns:xlink="http://www.w3.org/1999/xlink" '
    b'article-type="research-article">\n'
    b'<front><article-meta><title-group><article-title>'
    b'Demo</article-title></title-group></article-meta></front>'
    b'</article>'
)

_JATS_WITH_DOCTYPE_AND_COMMENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving '
    b'and Interchange DTD v1.3 20210610//EN" '
    b'"JATS-archivearticle1-3.dtd">\n'
    b'<!-- (c) Springer Nature, all rights reserved -->\n'
    b'<article>\n  <front/>\n</article>'
)

_JATS_NAMESPACED_ROOT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<jats:article xmlns:jats="https://jats.nlm.nih.gov/archiving/1.3/">'
    b'</jats:article>'
)

_JATS_WITH_BOM = b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n<article></article>'

_ELSEVIER = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd" '
    b'xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">\n'
    b'<coredata><dc:title>Foo</dc:title></coredata>\n'
    b'<originalText><xocs:doc/></originalText>\n'
    b'</full-text-retrieval-response>'
)

# The live ScienceDirect full-text API serves the body with *no* XML
# declaration — it starts straight at ``<full-text-retrieval-response>``.
# Sniffing must accept that (regression: it used to require ``<?xml``).
_ELSEVIER_NO_DECL = (
    b'<full-text-retrieval-response '
    b'xmlns="http://www.elsevier.com/xml/svapi/article/dtd" '
    b'xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">'
    b'<coredata><dc:title>Foo</dc:title></coredata>'
    b'<originalText><xocs:doc/></originalText>'
    b'</full-text-retrieval-response>'
)

_PAYWALL_HTML = (
    b'<!DOCTYPE html>\n'
    b'<html lang="en"><head><title>Sign in</title></head>'
    b'<body><h1>Access denied</h1></body></html>'
)

_HTML_WITH_XML_PRELUDE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
    b'"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n'
    b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Foo</title>'
    b'</head><body>not an article</body></html>'
)

_OTHER_XML_ROOT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<rss version="2.0"><channel><title>Feed</title></channel></rss>'
)


# classify --------------------------------------------------------------------


def test_classify_pdf_prefix() -> None:
    assert classify(_PDF_HEAD) is Format.PDF


def test_classify_pdf_with_garbage_prefix_rejected() -> None:
    # PDF spec lets readers skip up to 1 KiB of leading garbage, but TDM
    # endpoints and library-proxy sideloads return clean bytes; relaxing
    # this would weaken the paywall-HTML guard.
    assert classify(b'   ' + _PDF_HEAD) is None


def test_classify_jats_bare() -> None:
    assert classify(_JATS_BARE) is Format.JATS_XML


def test_classify_jats_with_doctype_and_comment() -> None:
    assert classify(_JATS_WITH_DOCTYPE_AND_COMMENT) is Format.JATS_XML


def test_classify_jats_namespaced_root_uses_local_name() -> None:
    assert classify(_JATS_NAMESPACED_ROOT) is Format.JATS_XML


def test_classify_jats_with_utf8_bom() -> None:
    assert classify(_JATS_WITH_BOM) is Format.JATS_XML


def test_classify_elsevier_full_text_envelope() -> None:
    assert classify(_ELSEVIER) is Format.ELSEVIER_XML


def test_classify_elsevier_without_xml_declaration() -> None:
    # The live ScienceDirect API omits the XML declaration; the root
    # element alone classifies it.
    assert classify(_ELSEVIER_NO_DECL) is Format.ELSEVIER_XML


def test_classify_paywall_html_returns_none() -> None:
    # Adversarial: paywall HTML body served at a `.pdf` URL.
    assert classify(_PAYWALL_HTML) is None


def test_classify_html_with_xml_prelude_returns_none() -> None:
    # Adversarial: real `<?xml` declaration but the document is XHTML.
    # Stripping PI + DOCTYPE leaves `<html>`, which is not in the table.
    assert classify(_HTML_WITH_XML_PRELUDE) is None


def test_classify_unknown_xml_root_returns_none() -> None:
    # An RSS feed has a valid XML decl but its root is `<rss>`.
    assert classify(_OTHER_XML_ROOT) is None


def test_classify_xml_without_declaration_accepted() -> None:
    # The XML declaration is optional in XML 1.0 and the ScienceDirect
    # full-text API omits it; the first opening tag is the discriminator.
    assert classify(b'<article></article>') is Format.JATS_XML
    assert classify(b'<full-text-retrieval-response/>') is Format.ELSEVIER_XML


def test_classify_empty_buffer() -> None:
    assert classify(b'') is None


def test_classify_short_buffer_below_pdf_magic() -> None:
    assert classify(b'%PD') is None


def test_classify_only_first_4kib_is_inspected() -> None:
    # Anything beyond the sniff window is ignored — even a valid PDF
    # magic that starts at byte 4096 must not be detected.
    prelude = b'\x00' * 4096
    assert classify(prelude + _PDF_HEAD) is None


def test_classify_jats_followed_by_extra_bytes() -> None:
    # The window-truncating side: a multi-MB JATS file still classifies
    # off its prefix.
    body = _JATS_BARE + b'\n' + b'x' * 10_000
    assert classify(body) is Format.JATS_XML


# verify ----------------------------------------------------------------------


def test_verify_pdf_round_trip(tmp_path: Path) -> None:
    p = tmp_path / 'a.pdf'
    p.write_bytes(_PDF_HEAD)
    verify(p, expected=Format.PDF, doi='10.1002/mrm.27973')


def test_verify_jats_round_trip(tmp_path: Path) -> None:
    p = tmp_path / 'a.xml'
    p.write_bytes(_JATS_BARE)
    verify(p, expected=Format.JATS_XML, doi='10.1186/s12880-024-12345-1')


def test_verify_elsevier_round_trip(tmp_path: Path) -> None:
    p = tmp_path / 'a.xml'
    p.write_bytes(_ELSEVIER)
    verify(p, expected=Format.ELSEVIER_XML, doi='10.1016/j.neuroimage.2024.01.001')


def test_verify_elsevier_without_declaration_round_trip(tmp_path: Path) -> None:
    # The shape the real ScienceDirect API returns — no XML declaration.
    # Regression: this used to raise MalformedArtifactError.
    p = tmp_path / 'a.xml'
    p.write_bytes(_ELSEVIER_NO_DECL)
    verify(p, expected=Format.ELSEVIER_XML, doi='10.1016/j.mri.2026.110656')


def test_verify_pdf_expected_but_html_raises(tmp_path: Path) -> None:
    p = tmp_path / 'a.pdf'
    p.write_bytes(_PAYWALL_HTML)
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.PDF, doi='10.1002/mrm.27973')
    err = exc_info.value
    assert err.doi == '10.1002/mrm.27973'
    assert err.context['expected'] == 'pdf'
    assert err.context['detected'] == 'unrecognized'
    assert err.context['byte_size'] == len(_PAYWALL_HTML)


def test_verify_jats_misclassified_as_elsevier_raises(tmp_path: Path) -> None:
    p = tmp_path / 'a.xml'
    p.write_bytes(_JATS_BARE)
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.ELSEVIER_XML, doi='10.1186/x')
    err = exc_info.value
    assert err.context['expected'] == 'elsevier_xml'
    assert err.context['detected'] == 'jats_xml'


def test_verify_elsevier_misclassified_as_jats_raises(tmp_path: Path) -> None:
    p = tmp_path / 'a.xml'
    p.write_bytes(_ELSEVIER)
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.JATS_XML, doi='10.1016/x')
    err = exc_info.value
    assert err.context['expected'] == 'jats_xml'
    assert err.context['detected'] == 'elsevier_xml'


def test_verify_html_with_xml_prelude_rejected_as_jats(tmp_path: Path) -> None:
    p = tmp_path / 'a.xml'
    p.write_bytes(_HTML_WITH_XML_PRELUDE)
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.JATS_XML, doi='10.1016/x')
    assert exc_info.value.context['detected'] == 'unrecognized'


def test_verify_empty_file_rejected(tmp_path: Path) -> None:
    p = tmp_path / 'a.pdf'
    p.write_bytes(b'')
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.PDF, doi='10.1002/x')
    assert exc_info.value.context['byte_size'] == 0


def test_verify_path_recorded_in_context(tmp_path: Path) -> None:
    p = tmp_path / 'subdir' / 'fetch.part'
    p.parent.mkdir()
    p.write_bytes(_PAYWALL_HTML)
    with pytest.raises(MalformedArtifactError) as exc_info:
        verify(p, expected=Format.PDF, doi='10.1002/x')
    assert exc_info.value.context['path'] == str(p)
