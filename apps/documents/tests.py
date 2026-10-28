from __future__ import annotations

import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from docx import Document as DocxDocument

from apps.accounts.models import User, UserRole
from apps.core.models import Institution
from apps.documents.extractors import DocumentTextExtractor
from apps.documents.models import Document, DocumentKind

DOCX_MIME = DocumentTextExtractor.DOCX_MIME


def build_docx_bytes(paragraphs: list[str]) -> bytes:
    docx = DocxDocument()

    for paragraph in paragraphs:
        docx.add_paragraph(paragraph)

    buffer = io.BytesIO()
    docx.save(buffer)
    return buffer.getvalue()


class DocxDocumentTestMixin:
    """
    Crea un `Document` DOCX real (archivo en un MEDIA_ROOT temporal) para
    probar la extracción y el pipeline sobre lo que produce python-docx.
    """

    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug=f"instituto-docx-{type(self).__name__.lower()}",
        )
        self.student = User.objects.create_user(
            username=f"alumno-docx-{type(self).__name__.lower()}",
            email=f"alumno-docx-{type(self).__name__.lower()}@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

    def tearDown(self) -> None:
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def make_docx_document(self, paragraphs: list[str]) -> Document:
        payload = build_docx_bytes(paragraphs)
        document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis DOCX",
            kind=DocumentKind.OTHER,
            original_filename="tesis.docx",
            mime_type=DOCX_MIME,
            file_size_bytes=len(payload),
            sha256_hash=f"hash-docx-{len(paragraphs)}-{len(payload)}",
        )
        document.original_file.save("tesis.docx", ContentFile(payload), save=False)
        document.save()
        return document


class DocxParagraphSeparationTests(DocxDocumentTestMixin, TestCase):
    """
    Cada párrafo de Word debe quedar separado por "\\n\\n": el filtro, la
    detección de IA y la búsqueda web parten por párrafos. Con "\\n" todo
    el DOCX era un solo bloque (tesis Mantilla: 0 % de similitud web).
    """

    PARAGRAPHS = [
        "Primer párrafo del documento con suficiente texto para contar.",
        "Segundo párrafo del documento, también con texto suficiente.",
        "Tercer párrafo del documento que cierra la prueba de extracción.",
    ]

    def test_docx_paragraphs_are_separated_by_a_blank_line(self) -> None:
        document = self.make_docx_document(self.PARAGRAPHS)

        result = DocumentTextExtractor().extract(document=document)

        self.assertEqual(result.content, "\n\n".join(self.PARAGRAPHS))

    def test_empty_docx_paragraphs_do_not_leave_extra_blank_lines(self) -> None:
        document = self.make_docx_document(
            [self.PARAGRAPHS[0], "", "   ", self.PARAGRAPHS[1], self.PARAGRAPHS[2]],
        )

        result = DocumentTextExtractor().extract(document=document)

        self.assertNotIn("\n\n\n", result.content)
        self.assertEqual(len(result.content.split("\n\n")), 3)
