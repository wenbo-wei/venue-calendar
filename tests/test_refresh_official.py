import unittest
from datetime import datetime, timezone

from scripts import refresh_official as refresh


AAAI = {
    "slug": "aaai",
    "title": "AAAI",
    "aliases": ["AAAI"],
    "trusted_hosts": ["aaai.org"],
}
ICLR = {
    "slug": "iclr",
    "title": "ICLR",
    "aliases": ["ICLR", "International Conference on Learning Representations"],
    "trusted_hosts": ["iclr.cc"],
}
WACV = {
    "slug": "wacv",
    "title": "WACV",
    "aliases": ["WACV", "Winter Conference on Applications of Computer Vision"],
    "location_layout": "heading_before_date",
}


class EditionValidationTests(unittest.TestCase):
    def test_accepts_matching_conference_and_year(self):
        page = """
        <html><head><title>AAAI-27</title></head>
        <body><h1>AAAI-27</h1><p>February 16-23, 2027 | Montréal, Canada</p></body></html>
        """
        self.assertEqual(refresh.validate_official_page(page, AAAI, 2027), (True, "verified"))

    def test_rejects_soft_404_even_when_identity_is_in_navigation(self):
        page = """
        <html><head><title>Page not found</title></head>
        <body><nav>AAAI 2027</nav><h1>404 - Page not found</h1></body></html>
        """
        accepted, reason = refresh.validate_official_page(page, AAAI, 2027)
        self.assertFalse(accepted)
        self.assertEqual(reason, "soft error page")

    def test_rejects_unidentified_year_placeholder(self):
        accepted, _ = refresh.validate_official_page("Welcome 2027.", AAAI, 2027)
        self.assertFalse(accepted)

    def test_trusted_host_uses_a_real_domain_boundary(self):
        self.assertTrue(refresh.trusted_host("https://events.aaai.org/path", AAAI))
        self.assertFalse(refresh.trusted_host("https://evilaaai.org/path", AAAI))

    def test_fetch_guard_rejects_loopback_and_metadata_addresses(self):
        for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                refresh.assert_public_url(url)

    def test_archive_body_is_not_promoted_without_a_target_heading(self):
        page = """
        <html><head><title>Conference archive</title></head>
        <body><p>Browse the AAAI 2027 proceedings.</p></body></html>
        """
        accepted, _ = refresh.validate_official_page(page, AAAI, 2027)
        self.assertFalse(accepted)

    def test_cfp_subpage_is_not_promoted_as_the_homepage(self):
        page = """
        <html><head><title>AAAI 2027 Call for Papers</title></head>
        <body><h1>AAAI 2027 Call for Papers</h1><p>Submission instructions</p></body></html>
        """
        accepted, reason = refresh.validate_official_page(page, AAAI, 2027)
        self.assertFalse(accepted)
        self.assertEqual(reason, "edition subpage rather than conference homepage")

    def test_program_subpage_is_not_promoted_as_the_homepage(self):
        page = """
        <html><head><title>ACM MM 2027 Program</title></head>
        <body><h1>ACM MM 2027 Program</h1></body></html>
        """
        source = {"slug": "mm", "title": "ACM MM", "aliases": ["ACM MM"]}
        accepted, _ = refresh.validate_official_page(page, source, 2027)
        self.assertFalse(accepted)


class LocationExtractionTests(unittest.TestCase):
    def test_extracts_schema_org_event_location(self):
        page = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Event",
          "name": "Example 2027",
          "location": {
            "@type": "Place",
            "name": "Palais des congrès",
            "address": {
              "addressLocality": "Montréal",
              "addressCountry": "Canada"
            }
          }
        }
        </script>
        """
        found = refresh.extract_location(page, 2027)
        self.assertEqual(found["display"], "Palais des congrès, Montréal, Canada")
        self.assertEqual(found["method"], "schema_org_event")

    def test_preserves_venue_words_in_structured_location(self):
        for location in (
            "Center for Innovation, Boston, MA",
            "National Conference Center, Leesburg, VA",
        ):
            with self.subTest(location=location):
                page = f"""
                <script type="application/ld+json">
                {{
                  "@type": "Event",
                  "name": "AAAI 2027",
                  "location": {{"@type": "Place", "name": "{location}"}}
                }}
                </script>
                """
                self.assertEqual(
                    refresh.extract_location(page, 2027, AAAI)["display"],
                    location,
                )

    def test_ignores_jsonld_for_a_different_event(self):
        page = """
        <script type="application/ld+json">
        {
          "@type": "Event",
          "name": "Unrelated Workshop 2027",
          "location": {"@type": "Place", "name": "Sponsor Hall"}
        }
        </script>
        """
        self.assertIsNone(refresh.extract_location(page, 2027, AAAI))

    def test_extracts_date_location_banner(self):
        page = "<p>February 16 – February 23, 2027 | Montréal, Canada</p>"
        self.assertEqual(refresh.extract_location(page, 2027)["display"], "Montréal, Canada")

    def test_extracts_location_before_event_date(self):
        page = """
        <h1>WACV 2027</h1>
        <h2>Disney Springs, Buena Vista, FL</h2>
        <h4>Monday, Jan 4 through Friday, Jan 8, 2027</h4>
        """
        self.assertEqual(
            refresh.extract_location(page, 2027, WACV)["display"],
            "Disney Springs, Buena Vista, FL",
        )

    def test_does_not_treat_important_dates_heading_as_a_location(self):
        page = "<h2>Important Dates</h2><p>January 4, 2027</p>"
        self.assertIsNone(refresh.extract_location(page, 2027))

    def test_does_not_publish_tba_as_a_location(self):
        for value in ("TBA", "TBC", "To be confirmed", "Coming soon"):
            with self.subTest(value=value):
                page = f"<h1>AAAI 2027</h1><p>Location: {value}</p>"
                self.assertIsNone(refresh.extract_location(page, 2027, AAAI))

    def test_does_not_treat_comma_heading_as_location(self):
        page = "<p>General Information, News, Updates</p><p>June 1, 2027</p>"
        self.assertIsNone(refresh.extract_location(page, 2027))

    def test_does_not_treat_navigation_after_edition_label_as_location(self):
        page = "<h1>Future meetings</h1><p>ICLR 2027</p><p>Privacy Policy</p>"
        self.assertIsNone(refresh.extract_series_location(page, ICLR, 2027))

    def test_future_meetings_location_is_bound_to_target_year(self):
        page = """
        <h1>Future ICLR meetings</h1>
        <p>ICLR 2026</p><p>Location: Old City</p>
        <p>ICLR 2027: New Region</p>
        """
        self.assertEqual(
            refresh.extract_series_location(page, ICLR, 2027)["display"],
            "New Region",
        )

    def test_does_not_treat_posted_in_a_menu_as_a_location(self):
        page = """
        <h1>WACV 2027</h1>
        <p>Important WACV 2027 announcements will appear here.
        Calls will be posted in the Calls menu above.</p>
        """
        self.assertIsNone(refresh.extract_location(page, 2027))

    def test_extracts_region_from_official_future_meetings_page(self):
        page = "<h1>Future ICLR Meetings</h1><p>ICLR 2027: West Coast North America</p>"
        found = refresh.extract_series_location(page, ICLR, 2027)
        self.assertEqual(found["display"], "West Coast North America")
        self.assertEqual(found["method"], "official_future_meeting")

    def test_location_change_requires_two_matching_observations(self):
        now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        prior = {"location": {"display": "Old City", "source_url": "https://aaai.org/old"}}
        extracted = {
            "display": "New City",
            "method": "schema_org_event",
            "confidence": "high",
            "evidence": "New City",
        }
        kept, pending = refresh.apply_location_stability(
            extracted, prior, now, "https://aaai.org/new"
        )
        self.assertEqual(kept["display"], "Old City")
        self.assertEqual(pending["observations"], 1)

        confirmed, pending = refresh.apply_location_stability(
            extracted,
            {**prior, "pending_location": pending},
            now,
            "https://aaai.org/new",
        )
        self.assertEqual(confirmed["display"], "New City")
        self.assertIsNone(pending)


class DiscoveryTests(unittest.TestCase):
    def test_official_hub_can_discover_an_external_edition_domain(self):
        page = '<a href="https://example-2027.org/">AAAI 2027 official conference</a>'
        links = refresh.extract_links(page, "https://aaai.org/conference/aaai/")
        self.assertTrue(refresh.probable_homepage_reference(*links[0], AAAI, 2027))

    def test_official_hub_subpage_is_not_a_homepage_candidate(self):
        for section in ("Venue", "News", "LocalInformation", "Hotels", "Program"):
            with self.subTest(section=section):
                url = f"https://example-2027.org/{section}"
                label = f"AAAI 2027 {section}"
                self.assertFalse(
                    refresh.probable_homepage_reference(url, label, AAAI, 2027)
                )

    def test_year_rules(self):
        self.assertEqual(refresh.edition_year("annual_next", 2026), 2027)
        self.assertEqual(refresh.edition_year("even_next", 2026), 2028)
        self.assertEqual(refresh.edition_year("odd_next", 2027), 2029)

    def test_deadline_date_must_follow_its_own_label(self):
        page = """
        <p>Author registration opens — June 30, 2026</p>
        <p>Paper submission deadline — July 28, 2026</p>
        """
        self.assertEqual(
            refresh.extract_deadlines(page, 2027)["deadline"],
            "2026-07-28 23:59:59",
        )

    def test_deadline_table_can_put_date_before_label(self):
        page = """
        <p>June 30, 2026<br>OpenReview submission site opens for paper submission</p>
        <p>July 21, 2026<br>Abstracts due at 11:59 PM UTC-12</p>
        <p>July 28, 2026<br>Full papers due at 11:59 PM UTC-12</p>
        <p>July 31, 2026<br>Supplementary material and code due</p>
        """
        self.assertEqual(
            refresh.extract_deadlines(page, 2027),
            {
                "abstract_deadline": "2026-07-21 23:59:59",
                "deadline": "2026-07-28 23:59:59",
            },
        )


if __name__ == "__main__":
    unittest.main()
