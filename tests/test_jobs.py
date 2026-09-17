"""Тесты парсера вакансий. Без сети и без БД портала.

Проверяется то, из-за чего сигнал найма молча врёт: разбор дат, отсев чужих
доменов и арифметика дельты.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobs import discover, domains, feeds, sources               # noqa: E402
from jobs.normalize import (Vacancy, country_from_location, parse_dt,       # noqa: E402
                            parse_relative_posted, seniority, workplace)
from jobs.store import Store                                     # noqa: E402


class FakeHttp:
    """Заглушка транспорта: отдаёт заранее заготовленный ответ."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def get_json(self, url, headers=None):
        self.calls.append(url)
        return self.payload

    def get_text(self, url, headers=None):
        self.calls.append(url)
        return self.payload

    def post_json(self, url, payload, headers=None):
        self.calls.append(url)
        return self.payload


class TestDates(unittest.TestCase):
    def test_iso_with_timezone(self):
        dt, precision = parse_dt("2026-07-14T08:29:20.852Z")
        self.assertEqual(precision, "exact")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 7, 14))
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_epoch_milliseconds(self):
        """Lever отдаёт createdAt в миллисекундах — в секундах это был бы 1970 год."""
        dt, precision = parse_dt(1753000000000)
        self.assertEqual(precision, "exact")
        self.assertGreater(dt.year, 2020)

    def test_date_only_keeps_precision(self):
        """Workable отдаёт дату без времени — «сегодня против вчера» по ней не считается."""
        dt, precision = parse_dt("2026-07-10")
        self.assertEqual(precision, "day")
        self.assertEqual(dt.hour, 0)

    def test_recruitee_utc_suffix(self):
        dt, _ = parse_dt("2026-08-05 11:22:33 UTC")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 8, 5, 11))

    def test_unparsable_is_none_not_now(self):
        """Ключевое: нераспознанная дата не подставляется «сегодня»,
        иначе старая вакансия станет свежим сигналом найма."""
        for value in (None, "", "как-нибудь потом", 12345, {}):
            dt, precision = parse_dt(value)
            self.assertIsNone(dt, f"{value!r} не должно давать дату")
            self.assertIsNone(precision)

    def test_workday_relative_labels(self):
        now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        today, precision = parse_relative_posted("Posted Today", now)
        self.assertEqual(today.date(), now.date())
        self.assertEqual(precision, "day")

        five, _ = parse_relative_posted("Posted 5 Days Ago", now)
        self.assertEqual(five.date(), (now - timedelta(days=5)).date())

        # '30+' — это не дата, а «давно»: верхней границы нет
        vague, precision = parse_relative_posted("Posted 30+ Days Ago", now)
        self.assertIsNone(vague)
        self.assertEqual(precision, "relative")


class TestGeoAndTitles(unittest.TestCase):
    def test_country_from_free_text(self):
        self.assertEqual(country_from_location("London, United Kingdom"), "gb")
        self.assertEqual(country_from_location("Austin, TX"), "us")
        self.assertEqual(country_from_location("München, Germany"), "de")
        self.assertIsNone(country_from_location("Кое-где"))

    def test_us_state_is_not_a_country(self):
        """Главная ловушка гео: двухбуквенный хвост — это чаще штат, чем страна."""
        self.assertEqual(country_from_location("San Francisco, CA"), "us")   # не Канада
        self.assertEqual(country_from_location("Wilmington, DE"), "us")      # не Германия
        self.assertEqual(country_from_location("Atlanta, GA"), "us")         # не Грузия
        self.assertEqual(country_from_location("Remote - Austin, TX (hybrid)"), "us")

    def test_real_country_codes_survive(self):
        self.assertEqual(country_from_location("Amsterdam, NL"), "nl")       # не Ньюфаундленд
        self.assertEqual(country_from_location("Bratislava, SK"), "sk")      # не Саскачеван
        self.assertEqual(country_from_location("Riyadh, SA"), "sa")
        self.assertEqual(country_from_location("Toronto, ON"), "ca")

    def test_workplace_flag_beats_text(self):
        self.assertEqual(workplace("Berlin", True), "remote")
        self.assertEqual(workplace("Remote - US"), "remote")
        self.assertEqual(workplace("Berlin (Hybrid)"), "hybrid")
        self.assertIsNone(workplace("Berlin"))

    def test_seniority_only_when_stated(self):
        self.assertEqual(seniority("Senior Backend Engineer"), "senior")
        self.assertEqual(seniority("Head of Marketing"), "c_level")
        self.assertEqual(seniority("Engineering Manager"), "manager")
        self.assertIsNone(seniority("Backend Engineer"))


class TestDomains(unittest.TestCase):
    def test_registrable_strips_service_subdomains(self):
        self.assertEqual(domains.registrable("https://careers.acme.co.uk/jobs"), "acme.co.uk")
        self.assertEqual(domains.registrable("www.acme.com"), "acme.com")

    def test_ats_hosts_rejected(self):
        """careers_url у большинства площадок указывает на саму площадку.
        Записать его доменом компании — значит написать письмо в Greenhouse."""
        for url in ("https://job-boards.greenhouse.io/stripe",
                    "https://jobs.lever.co/acme",
                    "https://acme.recruitee.com",
                    "https://acme.bamboohr.com/careers"):
            domain = domains.registrable(url)
            self.assertFalse(domains.is_company_domain(domain), url)

    def test_careers_hosting_rejected(self):
        """Найдено на живых данных: careerpuck.com был «доменом» сразу у семи компаний."""
        self.assertFalse(domains.is_company_domain("careerpuck.com"))
        self.assertFalse(domains.is_company_domain("comparably.com"))

    def test_custom_career_domain_accepted(self):
        self.assertTrue(domains.is_company_domain("vandebron.nl"))
        self.assertEqual(domains.registrable("https://werkenbij.vandebron.nl"), "vandebron.nl")

    def test_domain_from_description_requires_name_match(self):
        """Ни одна площадка не отдаёт сайт работодателя полем — остаётся текст
        вакансии. Но в нём же лежат LinkedIn, карты и сам ATS, поэтому домен
        засчитывается только при совпадении с названием компании."""
        text = ("Apply via https://boards.greenhouse.io/acme, follow us on "
                "https://linkedin.com/company/acme, learn more at https://acme.com/about")
        self.assertEqual(domains.from_description("Acme Technologies Inc", text),
                         ("acme.com", "description_name_match"))

    def test_domain_from_description_rejects_foreign_links(self):
        text = "See https://linkedin.com/company/acme and https://maps.google.com"
        self.assertEqual(domains.from_description("Acme", text), (None, None))

    def test_domain_from_description_needs_a_name(self):
        self.assertEqual(domains.from_description(None, "https://acme.com"), (None, None))
        self.assertEqual(domains.from_description("Acme", None), (None, None))

    def test_resolve_prefers_first_valid(self):
        domain, source = domains.resolve([
            ("careers_url", "https://jobs.lever.co/acme"),
            ("site_url", "https://acme.com"),
        ])
        self.assertEqual((domain, source), ("acme.com", "site_url"))


class TestAdapters(unittest.TestCase):
    def test_greenhouse_mapping(self):
        http = FakeHttp({"jobs": [{
            "id": 42, "title": "Senior Account Executive",
            "first_published": "2026-08-01T10:00:00Z",
            "updated_at": "2026-08-04T10:00:00Z",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/42",
            "company_name": "Acme", "departments": [{"name": "Sales"}],
            "offices": [{"location": "United States"}], "content": "&lt;p&gt;Text&lt;/p&gt;"}]})
        [vacancy] = sources.get("greenhouse").fetch(http, "acme")
        self.assertEqual(vacancy.source_job_id, "42")
        self.assertEqual(vacancy.country_code, "us")
        self.assertEqual(vacancy.department, "Sales")
        self.assertEqual(vacancy.seniority, "senior")
        self.assertEqual(vacancy.published_at.day, 1)      # first_published, не updated_at
        self.assertEqual(vacancy.description, "Text")      # двойной HTML-эскейп раскрыт

    def test_lever_epoch_and_workplace(self):
        http = FakeHttp([{"id": "abc", "text": "Backend Engineer",
                          "createdAt": 1753000000000, "country": "GB",
                          "categories": {"location": "London", "department": "Eng"},
                          "workplaceType": "remote",
                          "hostedUrl": "https://jobs.lever.co/acme/abc"}])
        [vacancy] = sources.get("lever").fetch(http, "acme")
        self.assertEqual(vacancy.country_code, "gb")
        self.assertEqual(vacancy.workplace, "remote")
        self.assertGreater(vacancy.published_at.year, 2020)

    def test_ashby_skips_unlisted(self):
        http = FakeHttp({"jobs": [
            {"id": "1", "title": "Visible", "isListed": True, "publishedAt": "2026-08-01T00:00:00Z"},
            {"id": "2", "title": "Hidden", "isListed": False}]})
        result = sources.get("ashby").fetch(http, "acme")
        self.assertEqual([v.source_job_id for v in result], ["1"])

    def test_rippling_dedupes_multilocation(self):
        """Rippling штатно повторяет вакансию по числу локаций с тем же uuid."""
        http = FakeHttp([
            {"uuid": "u1", "name": "AE", "workLocation": {"label": "New York"}},
            {"uuid": "u1", "name": "AE", "workLocation": {"label": "Boston"}},
            {"uuid": "u2", "name": "SDR", "workLocation": {"label": "Remote"}}])
        result = sources.get("rippling").fetch(http, "acme")
        self.assertEqual(len(result), 2)
        merged = next(v for v in result if v.source_job_id == "u1")
        self.assertIn("Boston", merged.location_raw)

    def test_bamboohr_has_no_date(self):
        http = FakeHttp({"result": [{"id": 7, "jobOpeningName": "Driver",
                                     "location": {"city": "Austin", "state": "TX"},
                                     "departmentLabel": "Ops"}]})
        [vacancy] = sources.get("bamboohr").fetch(http, "acme")
        self.assertIsNone(vacancy.published_at)
        self.assertEqual(vacancy.published_precision, "none")
        self.assertEqual(vacancy.country_code, "us")

    def test_workday_slug_must_be_composite(self):
        with self.assertRaises(ValueError):
            sources.get("workday").fetch(FakeHttp({}), "nvidia")


class TestDiscovery(unittest.TestCase):
    def test_url_to_source_and_slug(self):
        cases = {
            "https://job-boards.greenhouse.io/hungryroot/jobs/6115905004": ("greenhouse", "hungryroot"),
            "https://jobs.lever.co/safran-ai/d2e2ffee": ("lever", "safran-ai"),
            "https://jobs.ashbyhq.com/apollo-graphql/9e66ff59": ("ashby", "apollo-graphql"),
            "https://jobs.smartrecruiters.com/ALTEN/744000132837651": ("smartrecruiters", "ALTEN"),
            "https://extrashop.recruitee.com/o/vendeur": ("recruitee", "extrashop"),
            "https://mch-careers.breezy.hr/p/64db284f": ("breezy", "mch-careers"),
            "https://leitmotiv.teamtailor.com/jobs/8082680": ("teamtailor", "leitmotiv"),
            "https://nourish.bamboohr.com/careers/1256": ("bamboohr", "nourish"),
            "https://acme.jobs.personio.de/job/123": ("personio", "acme"),
            "https://ats.rippling.com/acme/jobs": ("rippling", "acme"),
        }
        for url, expected in cases.items():
            self.assertEqual(discover.from_url(url), expected, url)

    def test_workable_job_shortcode_is_not_a_company(self):
        """Регрессия: /j/CODE — это вакансия. Наивный шаблон завёл в реестр
        26 533 несуществующие «компании» из шорткодов."""
        self.assertIsNone(discover.from_url("https://apply.workable.com/j/04068221FA"))
        self.assertEqual(discover.from_url("https://apply.workable.com/ibmcid"),
                         ("workable", "ibmcid"))

    def test_workday_composite_slug(self):
        self.assertEqual(
            discover.from_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x"),
            ("workday", "nvidia|wd5|NVIDIAExternalCareerSite"))

    def test_smartrecruiters_case_is_preserved(self):
        """companyIdentifier чувствителен к регистру — приведение к нижнему ломает запрос."""
        source, slug = discover.from_url("https://careers.smartrecruiters.com/BoschGroup")
        self.assertEqual((source, slug), ("smartrecruiters", "BoschGroup"))

    def test_non_ats_url_ignored(self):
        self.assertIsNone(discover.from_url("https://europa.eu/eures/portal/jv-se/jv-details/X"))
        self.assertIsNone(discover.from_url(None))
        self.assertIsNone(discover.from_url("не ссылка"))

    def test_from_rows_dedupes(self):
        rows = [{"vacancy_url": "https://jobs.lever.co/acme/1"},
                {"vacancy_url": "https://jobs.lever.co/acme/2"},
                {"vacancy_url": "https://jobs.lever.co/other/1"}]
        found = list(discover.from_rows(rows))
        self.assertEqual(len(found), 2)
        self.assertEqual({f["slug"] for f in found}, {"acme", "other"})


class TestFeeds(unittest.TestCase):
    def test_remoteok_drops_legal_first_element(self):
        """Первый элемент ленты RemoteOK — юридическая приписка, а не вакансия."""
        http = FakeHttp([
            {"legal": "See remoteok.com/terms", "last_updated": "1"},
            {"id": 1, "company": "Acme Inc", "position": "AE",
             "date": "2026-08-01T10:00:00+00:00", "location": "Austin, TX"}])
        result = feeds.get("remoteok").fetch(http)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].slug, "acme-inc")
        self.assertEqual(result[0].workplace, "remote")
        self.assertEqual(result[0].country_code, "us")

    def test_himalayas_uses_company_slug(self):
        http = FakeHttp({"jobs": [{
            "title": "Consultant", "companyName": "iCodde", "companySlug": "icodde",
            "guid": "https://himalayas.app/companies/icodde/jobs/x",
            "applicationLink": "https://himalayas.app/companies/icodde/jobs/x",
            "pubDate": 1785935017, "locationRestrictions": ["Venezuela"],
            "seniority": ["Mid-level"]}]})
        [vacancy] = feeds.get("himalayas").fetch(http)
        self.assertEqual(vacancy.slug, "icodde")
        self.assertEqual(vacancy.seniority, "Mid-level")
        self.assertIsNotNone(vacancy.published_at)

    def test_company_slug_normalisation(self):
        self.assertEqual(feeds.company_slug("Coalition Technologies "), "coalition-technologies")
        self.assertEqual(feeds.company_slug("H&M Group"), "h-m-group")
        self.assertIsNone(feeds.company_slug(None))

    def test_arbeitnow_stops_without_next_link(self):
        http = FakeHttp({"data": [{"slug": "x", "company_name": "Acme", "title": "AE",
                                   "created_at": 1785934839, "location": "Munich"}],
                         "links": {"next": None}})
        result = feeds.get("arbeitnow").fetch(http, pages=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(http.calls), 1)      # вторая страница не запрашивалась


class TestFeedStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _feed_vacancy(job_id: str, published: datetime | None = None) -> Vacancy:
        return Vacancy(source="remoteok", slug="acme", source_job_id=job_id,
                       title="AE", published_at=published, country_code="us")

    def test_feed_never_closes_vacancies(self):
        """Лента — скользящее окно. Исчезновение вакансии из выдачи не значит,
        что её закрыли, и записывать закрытие было бы выдумкой."""
        fresh = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.upsert_companies([{"source": "remoteok", "slug": "acme",
                                      "company_name": "Acme", "domain": "acme.com"}])
        self.store.apply_feed_result([self._feed_vacancy("1", fresh),
                                      self._feed_vacancy("2", fresh)])
        # во второй выдаче второй вакансии нет — она просто уехала за край окна
        stats = self.store.apply_feed_result([self._feed_vacancy("1", fresh)])
        self.assertEqual(stats["new"], 0)
        still_open = self.store.conn.execute(
            "select count(*) from vacancy where closed_at is null").fetchone()[0]
        self.assertEqual(still_open, 2)

    def test_feed_row_without_date_is_baseline(self):
        self.store.upsert_companies([{"source": "remoteok", "slug": "acme"}])
        self.store.apply_feed_result([self._feed_vacancy("1")])
        self.assertEqual(self.store.hiring_signals(days=30), [])


class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")
        self.store.upsert_companies([
            {"source": "greenhouse", "slug": "acme", "company_name": "Acme",
             "domain": "acme.com"},
            {"source": "lever", "slug": "nodomain", "company_name": "No Domain"}])
        fresh = datetime.now(timezone.utc) - timedelta(days=3)
        old = datetime.now(timezone.utc) - timedelta(days=200)
        self.store.apply_company_result("greenhouse", "acme", [
            Vacancy(source="greenhouse", slug="acme", source_job_id="1", title="AE",
                    published_at=fresh, country_code="us"),
            Vacancy(source="greenhouse", slug="acme", source_job_id="2", title="SDR",
                    published_at=old, country_code="gb")])
        self.store.apply_company_result("lever", "nodomain", [
            Vacancy(source="lever", slug="nodomain", source_job_id="9", title="PM",
                    published_at=fresh, country_code="de")])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_export_all(self):
        self.assertEqual(len(list(self.store.vacancies())), 3)

    def test_export_filters(self):
        self.assertEqual(len(list(self.store.vacancies(days=30))), 2)
        self.assertEqual(len(list(self.store.vacancies(sources=["lever"]))), 1)
        self.assertEqual(len(list(self.store.vacancies(countries=["us", "gb"]))), 2)
        self.assertEqual(len(list(self.store.vacancies(require_domain=True))), 2)

    def test_export_joins_company_domain(self):
        rows = {r["source_job_id"]: r for r in self.store.vacancies()}
        self.assertEqual(rows["1"]["domain"], "acme.com")
        self.assertIsNone(rows["9"]["domain"])

    def test_company_keys_limit_export_to_matched_companies(self):
        """Регрессия: выгрузка вакансий без ограничения по компаниям берёт всё окно.
        На экране 657 компаний — в файле оказалось 33 487 строк, потому что
        min_new/max_new в выгрузку не передавались."""
        keys = [("greenhouse", "acme")]
        self.assertEqual(len(list(self.store.vacancies(company_keys=keys))), 2)
        self.assertEqual(len(list(self.store.vacancies())), 3)

    def test_empty_company_keys_export_nothing(self):
        """Пустой список компаний — это «ничего не подошло», а не «выгрузить всё»."""
        self.assertEqual(len(list(self.store.vacancies(company_keys=[]))), 0)

    def test_closed_excluded_by_default(self):
        self.store.apply_company_result("lever", "nodomain", [])   # вакансия 9 закрылась
        self.assertEqual(len(list(self.store.vacancies())), 2)
        self.assertEqual(len(list(self.store.vacancies(open_only=False))), 3)


def _vacancy(job_id: str, title: str = "AE", published: datetime | None = None) -> Vacancy:
    return Vacancy(source="greenhouse", slug="acme", source_job_id=job_id,
                   title=title, published_at=published, country_code="us")


class TestStoreDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")
        self.store.upsert_companies([{"source": "greenhouse", "slug": "acme",
                                      "company_name": "Acme", "domain": "acme.com"}])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_then_closed_then_reopened(self):
        first = self.store.apply_company_result("greenhouse", "acme",
                                                [_vacancy("1"), _vacancy("2")])
        self.assertEqual((first.new, first.closed), (2, 0))

        second = self.store.apply_company_result("greenhouse", "acme", [_vacancy("1")])
        self.assertEqual((second.new, second.closed), (0, 1))

        third = self.store.apply_company_result("greenhouse", "acme",
                                                [_vacancy("1"), _vacancy("2")])
        self.assertEqual((third.new, third.closed, third.reopened), (0, 0, 1))

    def test_duplicates_inside_one_payload(self):
        delta = self.store.apply_company_result(
            "greenhouse", "acme", [_vacancy("1"), _vacancy("1"), _vacancy("2")])
        self.assertEqual(delta.duplicates, 1)
        self.assertEqual(delta.seen, 2)

    def test_baseline_without_date_is_not_a_signal(self):
        """Первый обход компании на площадке без дат не должен выглядеть
        как пачка «только что появившихся» вакансий."""
        self.store.apply_company_result("greenhouse", "acme",
                                        [_vacancy("1"), _vacancy("2")], baseline=True)
        self.assertEqual(self.store.hiring_signals(days=30), [])

        self.store.apply_company_result("greenhouse", "acme",
                                        [_vacancy("1"), _vacancy("2"), _vacancy("3")])
        signals = self.store.hiring_signals(days=30)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["new_jobs"], 1)

    def test_dated_vacancy_counts_even_in_baseline(self):
        """А вот там, где площадка отдала дату, базовый срез сигналу не мешает:
        дата — факт от площадки, а не наше наблюдение."""
        fresh = datetime.now(timezone.utc) - timedelta(days=2)
        self.store.apply_company_result("greenhouse", "acme",
                                        [_vacancy("1", published=fresh)], baseline=True)
        self.assertEqual(len(self.store.hiring_signals(days=30)), 1)

    def test_old_vacancy_is_not_fresh(self):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        self.store.apply_company_result("greenhouse", "acme", [_vacancy("1", published=old)])
        self.assertEqual(self.store.hiring_signals(days=30), [])

    def test_geo_filter_reports_matched_job_countries(self):
        """Регрессия: в колонке гео должны стоять страны ОТОБРАННЫХ вакансий,
        а не страна компании из реестра. Иначе при фильтре «только US» в списке
        видно `gb` — компания британская, вакансия американская, — и фильтр
        выглядит сломанным."""
        self.store.upsert_companies([{"source": "greenhouse", "slug": "acme",
                                      "company_name": "Acme", "country_code": "gb"}])
        self.store.apply_company_result("greenhouse", "acme", [
            Vacancy(source="greenhouse", slug="acme", source_job_id="1",
                    title="AE", country_code="us"),
            Vacancy(source="greenhouse", slug="acme", source_job_id="2",
                    title="SDR", country_code="us"),
            Vacancy(source="greenhouse", slug="acme", source_job_id="3",
                    title="PM", country_code="de")])
        [row] = self.store.hiring_signals(days=30, countries=["us"])
        self.assertEqual(row["company_country"], "gb")     # карточка компании
        self.assertEqual(row["job_countries"], ["us"])     # а отобрано только US
        self.assertEqual(row["new_jobs"], 2)

    def test_total_matched_is_reported_beyond_limit(self):
        """Без общего числа выдача всегда упирается в лимит, и по ней не видно,
        что фильтр вообще что-то изменил."""
        for i in range(5):
            slug = f"c{i}"
            self.store.upsert_companies([{"source": "greenhouse", "slug": slug,
                                          "company_name": slug}])
            self.store.apply_company_result("greenhouse", slug, [
                Vacancy(source="greenhouse", slug=slug, source_job_id=f"{i}-1",
                        title="AE", country_code="us"),
                Vacancy(source="greenhouse", slug=slug, source_job_id=f"{i}-2",
                        title="SDR", country_code="us")])
        rows = self.store.hiring_signals(days=30, min_new=2, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["total_matched"], 5)
        # вакансий за этими компаниями — чтобы кнопка выгрузки не удивляла
        self.assertEqual(rows[0]["total_jobs"], 10)

    def test_max_new_filters_job_boards(self):
        many = [_vacancy(str(i)) for i in range(40)]
        self.store.apply_company_result("greenhouse", "acme", many)
        self.assertEqual(len(self.store.hiring_signals(days=30, min_new=1)), 1)
        self.assertEqual(self.store.hiring_signals(days=30, min_new=1, max_new=10), [])

    def test_require_domain(self):
        self.store.upsert_companies([{"source": "lever", "slug": "nodomain",
                                      "company_name": "No Domain"}])
        self.store.apply_company_result("lever", "nodomain", [
            Vacancy(source="lever", slug="nodomain", source_job_id="9", title="AE",
                    country_code="us")])
        self.store.apply_company_result("greenhouse", "acme", [_vacancy("1")])
        self.assertEqual(len(self.store.hiring_signals(days=30)), 2)
        self.assertEqual(len(self.store.hiring_signals(days=30, require_domain=True)), 1)

    def test_failed_company_does_not_close_vacancies(self):
        """Регрессия на главную ловушку дельты: 503 от площадки не должен
        выглядеть как «все вакансии компании закрылись»."""
        self.store.apply_company_result("greenhouse", "acme", [_vacancy("1"), _vacancy("2")])
        self.store.mark_company("greenhouse", "acme", ok=False, error="HTTP 503")
        open_count = self.store.conn.execute(
            "select count(*) from vacancy where closed_at is null").fetchone()[0]
        self.assertEqual(open_count, 2)

    def test_three_failures_deactivate_company(self):
        for _ in range(3):
            self.store.mark_company("greenhouse", "acme", ok=False, error="HTTP 404")
        row = self.store.conn.execute(
            "select active from company where source='greenhouse' and slug='acme'").fetchone()
        self.assertEqual(row["active"], 0)

    def test_upsert_never_overwrites_known_domain(self):
        self.store.upsert_companies([{"source": "greenhouse", "slug": "acme", "domain": None}])
        row = self.store.conn.execute(
            "select domain from company where slug='acme'").fetchone()
        self.assertEqual(row["domain"], "acme.com")


if __name__ == "__main__":
    unittest.main()
