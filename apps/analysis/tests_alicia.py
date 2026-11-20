from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.analysis.engines.alicia import (
    ALICIA_SUFFIX,
    AliciaFullTextHarvester,
    AliciaQuery,
    AliciaRecord,
    AliciaSearch,
    parse_record,
    rank_file_urls,
)
from apps.analysis.engines.oai_fulltext import (
    FetchedResponse,
    FullTextOutcome,
    OaiFullTextHarvester,
    RepositoryThrottled,
)
from apps.analysis.models import (
    DocumentFingerprint,
    OaiFullText,
    OaiFullTextStatus,
    OaiRecord,
)

MONTERRICO = "repositorio.monterrico.edu.pe"


def _api_record(number=1, **raw_overrides) -> dict:
    raw = {
        "id": f"EESPPM_{number}",
        "oai_identifier_str": f"oai:{MONTERRICO}:20.500.12905/{number}",
        "title": f"Comprensión lectora en educación primaria {number}",
        "author_facet": ["Pérez, Ana", "Quispe, Luis"],
        "description": "Resumen de la tesis sobre comprensión lectora.",
        "publishDate": ["2019"],
        "url": [f"https://hdl.handle.net/20.500.12905/{number}"],
        "dc.rights.uri.*.fl_str_mv": ["https://creativecommons.org/licenses/by/4.0/"],
        "repository.name.fl_str_mv": [
            "Repositorio de la Escuela Superior Pedagógica Pública Monterrico"
        ],
        "bitstream.url.fl_str_mv": [
            f"https://{MONTERRICO}/bitstreams/a{number}/download",
            f"https://{MONTERRICO}/bitstreams/b{number}/download",
        ],
    }
    raw.update(raw_overrides)
    return {"id": raw["id"], "rawData": raw, "dcDateEmbargoEnd": []}


def _alicia_oai_record(number, domain=MONTERRICO, urls=None) -> OaiRecord:
    return OaiRecord.objects.create(
        oai_identifier=f"oai:{domain}:x/{number}",
        repository_name=f"Repositorio {domain}{ALICIA_SUFFIX}",
        repository_domain=domain,
        title=f"Tesis {number}",
        source_url=f"https://hdl.handle.net/x/{number}",
        text_excerpt="Resumen",
        normalized_text="resumen",
        harvested_at=timezone.now(),
        extra_metadata={
            "fuente": "alicia",
            "bitstream_urls": urls
            if urls is not None
            else [f"https://{domain}/bitstreams/{number}/download"],
            "licencia": "https://creativecommons.org/licenses/by/4.0/",
            "anio": 2019,
        },
    )


class AliciaParseTests(SimpleTestCase):
    def test_record_keeps_origin_repository_files_and_license(self) -> None:
        record = parse_record(_api_record())

        self.assertEqual(record.oai_identifier, f"oai:{MONTERRICO}:20.500.12905/1")
        self.assertEqual(record.domain, MONTERRICO)
        self.assertEqual(record.year, 2019)
        self.assertEqual(record.authors, "Pérez, Ana; Quispe, Luis")
        self.assertEqual(record.license, "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(len(record.bitstream_urls), 2)
        self.assertIn("Monterrico", record.repository_name)

    def test_domain_falls_back_to_oai_identifier(self) -> None:
        record = parse_record(_api_record(**{"bitstream.url.fl_str_mv": []}))

        self.assertEqual(record.domain, MONTERRICO)
        self.assertEqual(record.bitstream_urls, [])

    def test_record_without_identifier_is_skipped(self) -> None:
        self.assertIsNone(parse_record(_api_record(oai_identifier_str="")))

    def test_embargo_end_is_read(self) -> None:
        raw = _api_record()
        raw["dcDateEmbargoEnd"] = ["2030-01-01"]

        self.assertEqual(parse_record(raw).embargo_end, date(2030, 1, 1))

    def test_query_filters_thesis_spanish_open_access(self) -> None:
        params = AliciaQuery(institutions=("EESPPM",), year_to=2021).params(page=2)

        self.assertEqual(params["lookfor"], "educación")
        self.assertEqual(params["type"], "Subject")
        self.assertEqual(params["page"], 2)
        self.assertIn('~format:"bachelorThesis"', params["filter[]"])
        self.assertIn('language:"spa"', params["filter[]"])
        self.assertIn('eu_rights_str_mv:"openAccess"', params["filter[]"])
        self.assertIn('~institution:"EESPPM"', params["filter[]"])
        self.assertIn('publishDate:"[* TO 2021]"', params["filter[]"])


class AliciaSearchTests(SimpleTestCase):
    def test_pages_until_a_short_page(self) -> None:
        client = mock.Mock()
        pages = [
            {"status": "OK", "records": [_api_record(1), _api_record(2)]},
            {"status": "OK", "records": [_api_record(3)]},
        ]
        client.get.side_effect = [
            FetchedResponse(200, "u", __import__("json").dumps(page).encode())
            for page in pages
        ]

        records = list(AliciaSearch(client=client).iter_records(AliciaQuery(page_size=2)))

        self.assertEqual(len(records), 3)
        self.assertEqual(client.get.call_count, 2)


class HarvestAliciaCommandTests(TestCase):
    def _call(self, records, **options) -> str:
        output = StringIO()

        with mock.patch.object(AliciaSearch, "iter_records", return_value=iter(records)):
            call_command("harvest_alicia", stdout=output, **options)

        return output.getvalue()

    def test_saves_new_records_with_links_and_fingerprints(self) -> None:
        self._call([parse_record(_api_record(1)), parse_record(_api_record(2))], limite=5)

        record = OaiRecord.objects.get(oai_identifier__endswith="/1")
        self.assertTrue(record.repository_name.endswith(ALICIA_SUFFIX))
        self.assertEqual(record.repository_domain, MONTERRICO)
        self.assertEqual(len(record.extra_metadata["bitstream_urls"]), 2)
        self.assertEqual(OaiRecord.objects.count(), 2)
        self.assertTrue(
            DocumentFingerprint.objects.filter(source_id=record.id, source_type="oai").exists()
        )

    def test_skips_existing_excluded_and_embargoed_and_respects_limit(self) -> None:
        _alicia_oai_record(99)
        existing = parse_record(_api_record(5))
        OaiRecord.objects.create(
            oai_identifier=existing.oai_identifier,
            repository_name="Repositorio UPeU",
            repository_domain=MONTERRICO,
            title="Ya estaba",
            text_excerpt="x",
            normalized_text="x",
            harvested_at=timezone.now(),
        )
        excluded = parse_record(
            _api_record(6, oai_identifier_str="oai:repositorio.ucv.edu.pe:1/6",
                        **{"bitstream.url.fl_str_mv": []})
        )
        embargoed = parse_record(_api_record(7))
        embargoed.embargo_end = timezone.localdate() + timedelta(days=30)
        records = [existing, excluded, embargoed] + [
            parse_record(_api_record(n)) for n in (10, 11, 12)
        ]

        output = self._call(records, limite=2)

        self.assertIn("'nuevos': 2", output)
        self.assertIn("'ya_existian': 1", output)
        self.assertIn("'dominio_excluido': 1", output)
        self.assertIn("'embargados': 1", output)
        self.assertEqual(OaiRecord.objects.get(oai_identifier=existing.oai_identifier).repository_name, "Repositorio UPeU")
        self.assertFalse(OaiRecord.objects.filter(oai_identifier__endswith="/12").exists())

    def test_dry_run_only_asks_the_total(self) -> None:
        output = StringIO()

        with mock.patch.object(AliciaSearch, "total", return_value=34767) as total:
            call_command("harvest_alicia", limite=1000, dry_run=True, stdout=output)

        total.assert_called_once()
        self.assertIn("34767", output.getvalue())
        self.assertFalse(OaiRecord.objects.exists())


class _FakeClient:
    def __init__(self, responses, disallowed=()) -> None:
        self.responses = responses
        self.disallowed = set(disallowed)
        self.calls = []

    def allowed(self, url):
        return url not in self.disallowed

    def get(self, url, *, max_bytes, params=None):
        self.calls.append(url)
        status, body, *content_type = self.responses[url]
        self.last_params = params
        return FetchedResponse(status, url, body, content_type[0] if content_type else "")


class AliciaFullTextHarvesterTests(TestCase):
    A = f"https://{MONTERRICO}/bitstreams/a/download"
    B = f"https://{MONTERRICO}/bitstreams/b/download"
    C = f"https://{MONTERRICO}/bitstreams/c/download"

    def _harvest(self, client, record, words_by_body):
        harvester = AliciaFullTextHarvester(client=client)

        def extract(pdf_bytes, base):
            words = words_by_body[pdf_bytes]
            status = OaiFullTextStatus.OK if words >= 1500 else OaiFullTextStatus.TEXTO_INSUFICIENTE
            return FullTextOutcome(status=status, content="palabra " * words if status == OaiFullTextStatus.OK else "", **base)

        with mock.patch.object(harvester, "_extract", side_effect=extract):
            return harvester.harvest(record)

    def test_skips_non_pdf_and_short_pdf_until_the_thesis(self) -> None:
        record = _alicia_oai_record(1, urls=[self.A, self.B, self.C])
        client = _FakeClient(
            {
                self.A: (200, b"licencia de texto"),
                self.B: (200, b"%PDF-autorizacion"),
                self.C: (200, b"%PDF-tesis"),
            }
        )

        full_text = self._harvest(
            client, record, {b"%PDF-autorizacion": 300, b"%PDF-tesis": 9000}
        )

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(full_text.pdf_url, self.C)
        self.assertEqual(full_text.publication_year, 2019)
        self.assertIn("creativecommons", full_text.rights)
        self.assertEqual(client.calls, [self.A, self.B, self.C])

    def test_robots_disallowing_every_link_is_final_without_downloading(self) -> None:
        record = _alicia_oai_record(2, urls=[self.A])
        client = _FakeClient({}, disallowed=[self.A])

        full_text = self._harvest(client, record, {})

        self.assertEqual(full_text.status, OaiFullTextStatus.SIN_PDF)
        self.assertIn("robots", full_text.error_message)
        self.assertEqual(client.calls, [])

    def test_no_pdf_among_links(self) -> None:
        record = _alicia_oai_record(3, urls=[self.A])
        client = _FakeClient({self.A: (404, b"")})

        full_text = self._harvest(client, record, {})

        self.assertEqual(full_text.status, OaiFullTextStatus.SIN_PDF)

    def test_without_links_uses_the_item_page_in_the_origin_repository(self) -> None:
        record = _alicia_oai_record(4, urls=[])
        landing = f"https://{MONTERRICO}/handle/x/4"
        pdf = f"https://{MONTERRICO}/bitstream/4.pdf"
        client = _FakeClient(
            {
                landing: (200, f'<meta name="citation_pdf_url" content="{pdf}">'.encode()),
                pdf: (200, b"%PDF-tesis"),
            }
        )

        full_text = self._harvest(client, record, {b"%PDF-tesis": 5000})

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(client.calls, [landing, pdf])


class HarvestOaiFullTextAliciaCommandTests(TestCase):
    def test_throttled_repository_is_left_for_later_and_others_continue(self) -> None:
        for number in range(1, 4):
            _alicia_oai_record(number, domain="lento.edu.pe")
            _alicia_oai_record(number + 10, domain="rapido.edu.pe")

        seen: list[str] = []

        def harvest(harvester, record):
            seen.append(record.repository_domain)

            if record.repository_domain == "lento.edu.pe":
                raise RepositoryThrottled("429")

            full_text, _ = OaiFullText.objects.update_or_create(
                record=record, defaults={"status": OaiFullTextStatus.OK}
            )
            return full_text

        output = StringIO()

        with mock.patch.object(OaiFullTextHarvester, "harvest", harvest):
            call_command(
                "harvest_oai_fulltext", repositorio="ALICIA", limite=10, stdout=output
            )

        self.assertEqual(seen.count("lento.edu.pe"), 1)
        self.assertEqual(seen.count("rapido.edu.pe"), 3)
        self.assertIn("Pendiente para otra corrida: lento.edu.pe", output.getvalue())
        self.assertEqual(OaiFullText.objects.count(), 3)

    def test_records_alternate_between_repositories(self) -> None:
        for number in range(1, 3):
            _alicia_oai_record(number, domain="a.edu.pe")
            _alicia_oai_record(number + 10, domain="b.edu.pe")

        seen: list[str] = []

        def harvest(harvester, record):
            seen.append(record.repository_domain)
            full_text, _ = OaiFullText.objects.update_or_create(
                record=record, defaults={"status": OaiFullTextStatus.SIN_PDF}
            )
            return full_text

        with mock.patch.object(OaiFullTextHarvester, "harvest", harvest):
            call_command(
                "harvest_oai_fulltext", repositorio="ALICIA", limite=10, stdout=StringIO()
            )

        self.assertEqual(seen, ["a.edu.pe", "b.edu.pe", "a.edu.pe", "b.edu.pe"])

    def test_only_alicia_records_are_selected(self) -> None:
        _alicia_oai_record(1)
        OaiRecord.objects.create(
            oai_identifier="oai:repositorio.upeu.edu.pe:1",
            repository_name="Repositorio UPeU",
            repository_domain="repositorio.upeu.edu.pe",
            title="UPeU",
            source_url="https://hdl.handle.net/1",
            text_excerpt="x",
            normalized_text="x",
            harvested_at=timezone.now(),
        )
        output = StringIO()

        call_command(
            "harvest_oai_fulltext", repositorio="ALICIA", limite=10, dry_run=True, stdout=output
        )

        self.assertIn("1 registros", output.getvalue())


class AliciaRecordDataclassTests(SimpleTestCase):
    def test_defaults(self) -> None:
        record = AliciaRecord("i", "oai:x:1", "t", "", "", "", "x", "x", None, "")
        self.assertEqual(record.bitstream_urls, [])


HTML = (200, b"<!doctype html><html></html>", "text/html")
JSON = "application/hal+json"
UMCH = "https://repositorio.umch.edu.pe"


class RankFileUrlsTests(SimpleTestCase):
    def test_drops_derivatives_and_moves_side_files_last(self) -> None:
        urls = [
            f"{UMCH}/bitstream/1/5/2/license.txt",
            f"{UMCH}/bitstream/1/5/3/Ficha%20de%20autorizaci%c3%b3n.pdf",
            f"{UMCH}/bitstream/1/5/4/tesis.pdf.txt",
            f"{UMCH}/bitstream/1/5/5/tesis.pdf.jpg",
            f"{UMCH}/bitstream/1/5/1/TESIS%20COMPLETA.pdf",
            f"{UMCH}/bitstreams/{'a' * 8}-aaaa-aaaa-aaaa-{'a' * 12}/download",
        ]

        ranked = rank_file_urls(urls)

        self.assertEqual(len(ranked), 3)
        self.assertTrue(ranked[0].endswith("TESIS%20COMPLETA.pdf"))
        self.assertIn("autorizaci", ranked[-1])


class AliciaDspace7Tests(TestCase):
    UUID = "745bbe30-9689-48c3-a005-5989e8b45981"

    def _harvest(self, client, record):
        harvester = AliciaFullTextHarvester(client=client)
        ok = FullTextOutcome(status=OaiFullTextStatus.OK, content="palabra " * 2000)

        with mock.patch.object(
            harvester,
            "_extract",
            side_effect=lambda pdf, base: FullTextOutcome(
                status=ok.status, content=ok.content, **base
            ),
        ):
            return harvester, harvester.harvest(record)

    def test_legacy_link_is_resolved_through_the_rest_api(self) -> None:
        legacy = f"{UMCH}/bitstream/20.500.14231/457/1/tesis.pdf"
        record = _alicia_oai_record(1, domain="repositorio.umch.edu.pe", urls=[legacy])
        content = f"{UMCH}/backend/api/core/bitstreams/{self.UUID}/content"
        client = _FakeClient(
            {
                legacy: HTML,
                f"{UMCH}/server/api": HTML,
                f"{UMCH}/backend/api": (200, b"{}", JSON),
                f"{UMCH}/backend/api/core/bitstreams/search/byItemHandle": (
                    200,
                    ('{"_links": {"content": {"href": "%s"}}}' % content).encode(),
                    JSON,
                ),
                content: (200, b"%PDF-tesis", "application/pdf"),
            }
        )

        _, full_text = self._harvest(client, record)

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(full_text.pdf_url, content)
        self.assertEqual(
            client.calls[-2], f"{UMCH}/backend/api/core/bitstreams/search/byItemHandle"
        )

    def test_uuid_download_link_goes_to_rest_content_and_host_is_remembered(self) -> None:
        first = f"{UMCH}/bitstreams/{self.UUID}/download"
        other_uuid = "11111111-2222-3333-4444-555555555555"
        second = f"{UMCH}/bitstreams/{other_uuid}/download"
        records = [
            _alicia_oai_record(1, domain="repositorio.umch.edu.pe", urls=[first]),
            _alicia_oai_record(2, domain="repositorio.umch.edu.pe", urls=[second]),
        ]
        client = _FakeClient(
            {
                first: HTML,
                f"{UMCH}/server/api": (200, b"{}", JSON),
                f"{UMCH}/server/api/core/bitstreams/{self.UUID}/content": (200, b"%PDF-1"),
                f"{UMCH}/server/api/core/bitstreams/{other_uuid}/content": (200, b"%PDF-2"),
            }
        )
        harvester, full_text = self._harvest(client, records[0])

        with mock.patch.object(
            harvester,
            "_extract",
            side_effect=lambda pdf, base: FullTextOutcome(
                status=OaiFullTextStatus.OK, content="palabra " * 2000, **base
            ),
        ):
            second_text = harvester.harvest(records[1])

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(second_text.status, OaiFullTextStatus.OK)
        self.assertNotIn(second, client.calls)
        self.assertEqual(client.calls.count(f"{UMCH}/server/api"), 1)

    def test_broken_rest_api_ends_as_sin_pdf(self) -> None:
        link = f"{UMCH}/bitstreams/{self.UUID}/download"
        record = _alicia_oai_record(1, domain="repositorio.umch.edu.pe", urls=[link])
        client = _FakeClient(
            {
                link: HTML,
                f"{UMCH}/server/api": (200, b"{}", JSON),
                f"{UMCH}/server/api/core/bitstreams/{self.UUID}/content": (500, b"{}", JSON),
            }
        )

        _, full_text = self._harvest(client, record)

        self.assertEqual(full_text.status, OaiFullTextStatus.SIN_PDF)


class ReintentarSinPdfTests(TestCase):
    def test_sin_pdf_records_are_retried_only_with_the_flag(self) -> None:
        record = _alicia_oai_record(1)
        OaiFullText.objects.create(record=record, status=OaiFullTextStatus.SIN_PDF)
        output = StringIO()

        call_command(
            "harvest_oai_fulltext", repositorio="ALICIA", limite=5, dry_run=True, stdout=output
        )
        call_command(
            "harvest_oai_fulltext",
            repositorio="ALICIA",
            limite=5,
            dry_run=True,
            reintentar_sin_pdf=True,
            stdout=output,
        )

        self.assertIn("0 por procesar", output.getvalue())
        self.assertIn("1 por procesar", output.getvalue())
