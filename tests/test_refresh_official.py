"""Offline regressions based on the official 2027 conference page layouts."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_official as refresh


NOW = datetime(2026, 9, 23, 5, tzinfo=timezone.utc)
SOURCE = {
    "slug": "cvpr", "title": "CVPR", "aliases": ["CVPR"],
    "description": "Computer Vision and Pattern Recognition", "rank": "A",
    "year_rule": "annual_next", "series_url": "https://cvpr.thecvf.com/",
    "trusted_hosts": ["thecvf.com"],
    "url": "https://cvpr.thecvf.com/Conferences/{year}",
}
HOME_URL = SOURCE["url"].format(year=2027)


def page(document, url=HOME_URL):
    return refresh.Page(url, url, document, 200)


def row(label, date):
    return f"<tr><td>{label}</td><td>{date}</td></tr>"


def countdown(name, value):
    return f'<script>var {name} = "{value} UTC";</script>'


class DeadlineExtractionTests(unittest.TestCase):
    def test_cvpr_static_utc_countdowns_convert_to_correct_aoe_day(self):
        document = countdown("paper_registration_deadline_1", "2026/11/11 11:59:59")
        document += countdown("submission_deadline_1", "2026/11/17 11:59:59")
        document += countdown("supplementary_materials_deadline_1", "2026/11/24 11:59:59")
        self.assertEqual(refresh.extract_deadlines(document, 2027), {
            "abstract_deadline": "2026-11-10 23:59:59", "deadline": "2026-11-16 23:59:59",
        })

    def test_iclr_duplicate_countdowns_and_visible_table_are_one_fact(self):
        document = countdown("paper_deadline", "2026/09/26 11:59:59") * 2
        document += row("Paper Deadline", "Sep 25 '26 (Anywhere on Earth)")
        found = refresh.select_deadlines(refresh.extract_deadline_observations(document, 2027))
        self.assertEqual(found["deadline"]["value"], "2026-09-25 23:59:59")
        self.assertEqual(found["deadline"]["method"], "official_countdown")

    def test_wacv_latest_round_uses_matching_registration_not_round_one(self):
        document = countdown("round_1_paper_submission_deadline_1", "2026/06/27 11:59:59")
        document += countdown("round_1_paper_registration_deadline_1", "2026/06/20 11:59:59")
        document += row("Round 2 New Paper Registration", "Aug 21 '26 (Anywhere on Earth)")
        document += row("Round 2 Paper Submissions", "Aug 28 '26 (Anywhere on Earth)")
        document += row("Round 1 Rebuttal and Revision Submission", "Aug 28 '26 (Anywhere on Earth)")
        self.assertEqual(refresh.extract_deadlines(document, 2027), {
            "deadline": "2026-08-28 23:59:59", "abstract_deadline": "2026-08-21 23:59:59",
        })

    def test_missing_second_round_abstract_does_not_borrow_round_one(self):
        document = row("Round 1 Paper Registration", "Jun 19 2026 AoE")
        document += row("Round 2 Paper Submission", "Aug 28 2026 AoE")
        self.assertNotIn("abstract_deadline", refresh.extract_deadlines(document, 2027))

    def test_explicit_clock_and_fixed_timezone_preserved(self):
        result = refresh.extract_deadlines(row("Paper Deadline", "September 25, 2026 at 18:30 UTC+2"), 2027)
        self.assertEqual(result["deadline"], "2026-09-25 04:30:00")

    def test_half_hour_offset_is_not_mistaken_for_cutoff_clock(self):
        self.assertEqual(refresh.extract_deadlines(row("Paper Deadline", "September 25, 2026 UTC+05:30"), 2027), {})
        result = refresh.extract_deadlines(row("Paper Deadline", "September 25, 2026 at 18:30 UTC+05:30"), 2027)
        self.assertEqual(result["deadline"], "2026-09-25 01:00:00")

    def test_date_without_timezone_is_not_fabricated(self):
        self.assertEqual(refresh.extract_deadlines(row("Paper Deadline", "September 23, 2026"), 2027), {})
        self.assertEqual(refresh.extract_deadlines(row("Paper Deadline", "September 23, 2026 UTC"), 2027), {})

    def test_one_rows_timezone_does_not_supply_an_unrelated_rows_timezone(self):
        document = row("Abstract Deadline", "September 18, 2026 AoE")
        document += row("Paper Deadline", "September 25, 2026")
        self.assertEqual(refresh.extract_deadlines(document, 2027), {"abstract_deadline": "2026-09-18 23:59:59"})

    def test_reviewed_registry_timezone_is_recorded_for_date_only_page(self):
        result = refresh.extract_deadline_observations(row("Full Paper Submission Deadline", "September 23, 2026"), 2027, "UTC-12")
        self.assertEqual(result[0]["value"], "2026-09-23 23:59:59")
        self.assertEqual(result[0]["timezone_source"], "registry")

    def test_global_aoe_and_nested_date_before_label(self):
        document = '<article><h2>Main Conference Timetable</h2><p>All deadlines are anywhere on earth (UTC-12)</p>'
        document += '<p><strong>July 21, 2026<br></strong>Abstracts due at 11:59 PM UTC-12</p></article>'
        result = refresh.extract_deadlines(document, 2027)
        self.assertEqual(result["abstract_deadline"], "2026-07-21 23:59:00")

    def test_struck_out_old_date_is_replaced_by_extension(self):
        document = row("Paper Submission Deadline", "<del>September 16, 2026</del> September 23, 2026 AoE")
        self.assertEqual(refresh.extract_deadlines(document, 2027)["deadline"], "2026-09-23 23:59:59")

    def test_ambiguous_multiple_dates_invalid_dates_and_wrong_year_rejected(self):
        for value in ("September 16, 2026 or September 23, 2026 AoE", "February 31, 2026 AoE", "September 16, 2025 AoE"):
            with self.subTest(value=value):
                self.assertEqual(refresh.extract_deadlines(row("Paper Deadline", value), 2027), {})

    def test_workshop_final_camera_ready_and_commented_deadlines_excluded(self):
        document = row("Full Paper Submission Deadline", "September 23, 2026 AoE")
        for label in ("Final Paper Deadline", "Workshop Paper Deadline", "Camera-ready Paper Deadline"):
            document += row(label, "January 27, 2027 AoE")
        document += "<!--" + row("Paper Deadline", "February 27, 2027 AoE") + "-->"
        document += "<h2>Workshops</h2>" + row("Paper Deadline", "March 27, 2027 AoE")
        self.assertEqual(refresh.extract_deadlines(document, 2027), {"deadline": "2026-09-23 23:59:59"})


class DeadlineDiscoveryTests(unittest.TestCase):
    def test_shared_society_host_cannot_supply_another_venues_dates(self):
        source = {**SOURCE, "title": "ICASSP", "slug": "icassp", "aliases": ["ICASSP"],
                  "url": "https://{year}.ieeeicassp.org/", "trusted_hosts": ["signalprocessingsociety.org", "ieeeicassp.org"]}
        home = page('<h1>ICASSP 2027</h1><a href="/events/icip-2027/dates">Important Dates</a>',
                    "https://signalprocessingsociety.org/events/icassp-2027")
        with patch.object(refresh, "fetch") as fetch:
            pages, errors = refresh.deadline_pages(home, source, 2027)
        self.assertEqual(pages, [home])
        fetch.assert_not_called()

    def test_scoped_url_with_another_venues_title_is_rejected(self):
        home = page('<h1>CVPR 2027</h1><a href="Dates">Dates</a>', HOME_URL + "/")
        wrong = page('<h1>ICCV 2027 Dates</h1>' + row("Paper Deadline", "March 2 2027 AoE"), HOME_URL + "/Dates")
        with patch.object(refresh, "fetch", return_value=wrong):
            pages, errors = refresh.deadline_pages(home, SOURCE, 2027)
        self.assertEqual(pages, [home])
        self.assertTrue(errors)

    def test_yearless_dates_on_verified_dedicated_host_are_accepted(self):
        source = {**SOURCE, "title": "ICASSP", "slug": "icassp", "aliases": ["ICASSP"],
                  "url": "https://{year}.ieeeicassp.org/", "trusted_hosts": ["ieeeicassp.org"]}
        home = page('<h1>ICASSP 2027</h1><a href="/important-dates/">Important Dates</a>', "https://2027.ieeeicassp.org/")
        dates = page('<h1>Important Dates</h1>' + row("Paper Deadline", "September 23 2026 AoE"), "https://2027.ieeeicassp.org/important-dates/")
        with patch.object(refresh, "fetch", return_value=dates):
            pages, errors = refresh.deadline_pages(home, source, 2027)
        self.assertIn(dates, pages)
        self.assertEqual(errors, [])

    def test_only_trusted_same_edition_main_paper_links(self):
        home = page("<h1>CVPR 2027</h1>")
        self.assertTrue(refresh.deadline_reference(HOME_URL + "/Dates", "Dates", home, SOURCE, 2027))
        for url, label in (
            ("https://evil.example/2027/Dates", "Dates"),
            ("http://127.0.0.1/Dates", "Dates"),
            ("https://iccv.thecvf.com/Conferences/2027/Dates", "Dates"),
            (HOME_URL.replace("2027", "2026") + "/Dates", "Dates"),
            (HOME_URL + "/Workshops/CallForPapers", "Workshop CFP"),
            (HOME_URL + "/news/updates", "Updates"),
            ("https://cvpr.thecvf.com/Dates?year=2025", "Dates"),
        ):
            with self.subTest(url=url):
                self.assertFalse(refresh.deadline_reference(url, label, home, SOURCE, 2027))

    def test_follow_dates_link_and_reject_redirected_wrong_edition(self):
        home = page('<h1>CVPR 2027</h1><a href="/Conferences/2027/Dates">Dates</a>')
        wrong = refresh.Page(HOME_URL + "/Dates", HOME_URL.replace("2027", "2026") + "/Dates", "<title>2026 Dates</title>", 200)
        with patch.object(refresh, "fetch", return_value=wrong):
            pages, errors = refresh.deadline_pages(home, SOURCE, 2027)
        self.assertEqual(pages, [home])
        self.assertIn("outside the target edition", errors[0])

    def test_wrong_year_content_and_unidentified_cross_host_rejected(self):
        home = page('<h1>CVPR 2027</h1><a href="/Conferences/2027/Dates">Dates</a>')
        with patch.object(refresh, "fetch", return_value=page("<title>2026 Dates</title>", HOME_URL + "/Dates")):
            pages, errors = refresh.deadline_pages(home, SOURCE, 2027)
        self.assertEqual(pages, [home])
        self.assertTrue(errors)

    def test_crawl_is_bounded_and_deduplicated(self):
        links = "".join(f'<a href="/Conferences/2027/Dates/{i}">Dates</a>' for i in range(20))
        home = page("<h1>CVPR 2027</h1>" + links)
        with patch.object(refresh, "fetch", side_effect=lambda url: page("<title>2027 Dates</title>" + links, url)) as fetch:
            pages, errors = refresh.deadline_pages(home, SOURCE, 2027)
        self.assertEqual(fetch.call_count, refresh.MAX_DEADLINE_PAGES)
        self.assertEqual(len(pages), refresh.MAX_DEADLINE_PAGES + 1)
        self.assertEqual(errors, [])

    def test_dedicated_site_replaces_cached_society_listing(self):
        source = {**SOURCE, "series_url": "https://society.example/events", "url": "https://2027.conference.example/", "trusted_hosts": ["society.example", "conference.example"]}
        prior = {"year": 2027, "official_url": "https://society.example/events/cvpr-2027"}
        with patch.object(refresh, "fetch", return_value=page("No links", source["series_url"])), patch.object(refresh, "discover_from_sitemaps", return_value=[]):
            candidates, _ = refresh.discovery_candidates(source, 2027, prior)
        self.assertEqual(candidates[0].url, source["url"])


class RefreshRetentionTests(unittest.TestCase):
    def test_new_round_drops_incompatible_fallback_abstract(self):
        dates = page(row("Round 2 Paper Submission", "Aug 28 2026 AoE"))
        prior = {"deadlines": {"deadline": "2026-06-26 23:59:59", "abstract_deadline": "2026-06-19 23:59:59"},
                 "timezone": "UTC-12", "deadline_evidence": {"abstract_deadline": {"round": 1}}}
        merged, evidence, status = refresh.merge_deadlines(SOURCE, prior, 2027, [dates], NOW)
        self.assertEqual(merged, {"deadline": "2026-08-28 23:59:59"})
        self.assertNotIn("abstract_deadline", evidence)
        known_source = {**SOURCE, "known": {2027: {**prior["deadlines"], "timezone": "UTC-12"}}}
        merged, evidence, _ = refresh.merge_deadlines(known_source, {}, 2027, [dates], NOW)
        self.assertNotIn("abstract_deadline", merged)
        next_prior = {"deadlines": merged, "deadline_evidence": evidence, "timezone": "UTC-12"}
        retained, _, _ = refresh.merge_deadlines(known_source, next_prior, 2027, [], NOW)
        self.assertEqual(retained, merged, "a failed check must preserve the validated round without restoring registry fields")
        merged, _, _ = refresh.merge_deadlines(known_source, next_prior, 2027, [dates], NOW)
        self.assertNotIn("abstract_deadline", merged, "obsolete registry abstract must not reappear on the next daily check")

    def test_new_round_preserves_a_matching_fallback_pair(self):
        dates = page(row("Round 2 Paper Submission", "Aug 28 2026 AoE"))
        known = {"deadline": "2026-08-28 23:59:59", "abstract_deadline": "2026-08-21 23:59:59", "timezone": "UTC-12"}
        source = {**SOURCE, "known": {2027: known}}
        merged, _, _ = refresh.merge_deadlines(source, {}, 2027, [dates], NOW)
        self.assertEqual(merged["abstract_deadline"], known["abstract_deadline"])

    def test_new_official_extension_overrides_known_and_previous_values(self):
        source = {**SOURCE, "known": {2027: {"deadline": "2026-09-16 23:59:59", "timezone": "UTC-12"}}}
        prior = {"deadlines": {"deadline": "2026-09-16 23:59:59"}, "timezone": "UTC-12"}
        deadlines, evidence, status = refresh.merge_deadlines(source, prior, 2027, [page(row("Full Paper Submission Deadline", "September 23, 2026"))], NOW)
        self.assertEqual(deadlines["deadline"], "2026-09-23 23:59:59")
        self.assertEqual(evidence["deadline"]["source_url"], HOME_URL)
        self.assertEqual(status, "verified")

    def test_retained_fields_convert_independently_when_new_page_uses_utc(self):
        prior = {"deadlines": {"abstract_deadline": "2026-09-19 11:59:59"}, "timezone": "UTC"}
        document = countdown("paper_deadline", "2026/09/26 11:59:59")
        deadlines, evidence, status = refresh.merge_deadlines(SOURCE, prior, 2027, [page(document)], NOW)
        self.assertEqual(deadlines, {"abstract_deadline": "2026-09-18 23:59:59", "deadline": "2026-09-25 23:59:59"})
        self.assertEqual(status, "partial")
        self.assertEqual(evidence["abstract_deadline"]["status"], "retained")

    def test_unparseable_page_and_failed_fetch_retain_verified_data(self):
        prior = {"year": 2027, "official_url": HOME_URL, "timezone": "UTC-12", "deadlines": {"deadline": "2026-11-16 23:59:59"}, "deadline_evidence": {"deadline": {"source_url": HOME_URL + "/Dates", "verified_at": "earlier"}}}
        deadlines, evidence, status = refresh.merge_deadlines(SOURCE, prior, 2027, [page("New layout without a parseable date")], NOW)
        self.assertEqual(deadlines, prior["deadlines"])
        self.assertEqual(evidence["deadline"]["verified_at"], "earlier")
        self.assertEqual(status, "retained")
        with patch.object(refresh, "discovery_candidates", return_value=([], ["network unavailable"])):
            venue, state = refresh.refresh_source(SOURCE, prior, NOW)
        self.assertEqual(state["deadlines"], prior["deadlines"])
        self.assertEqual(venue["confs"][0]["deadline_source_url"], HOME_URL + "/Dates")
        self.assertEqual(state["deadline_status"], "retained")

    def test_january_retains_current_edition_with_open_or_unknown_deadline(self):
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        for rule in ("annual_next", "odd_next"):
            source = {**SOURCE, "year_rule": rule}
            for deadlines in ({}, {"deadline": "2027-03-01 23:59:59"}):
                self.assertEqual(refresh.refresh_year(source, {"year": 2027, "deadlines": deadlines, "timezone": "UTC-12"}, now), 2027)

    def test_rollover_obeys_deadline_timezone_and_does_not_pin_past_year(self):
        prior = {"year": 2027, "deadlines": {"deadline": "2027-01-01 23:59:59"}, "timezone": "UTC-12"}
        self.assertEqual(refresh.refresh_year(SOURCE, prior, datetime(2027, 1, 2, 10, tzinfo=timezone.utc)), 2027)
        self.assertEqual(refresh.refresh_year(SOURCE, prior, datetime(2027, 1, 2, 13, tzinfo=timezone.utc)), 2028)
        self.assertEqual(refresh.refresh_year(SOURCE, {"year": 2026}, datetime(2027, 1, 1, tzinfo=timezone.utc)), 2028)


class LocationTests(unittest.TestCase):
    def test_official_series_preserves_both_announced_ijcai_locations(self):
        source = {"slug": "ijcai", "title": "IJCAI"}
        document = '<p>IJCAI-27 will be held from August 7th until August 13, 2027 in Kyoto, Japan and from August 15th until August 17, 2027 in Hengqin, China. We are looking forward to welcoming you there.</p>'
        result = refresh.extract_series_location(document, source, 2027)
        self.assertEqual(result["display"], "Kyoto, Japan; Hengqin, China")

    def test_hero_badge_precedes_venue_placeholder(self):
        document = '<h1>ICLR 2027</h1><div><i class="fas fa-map-marker-alt me-2"></i><a><span>California</span></a></div><h2>Venue</h2><p>Venue information will be posted here</p>'
        self.assertEqual(refresh.extract_location(document, 2027)["display"], "California")

    def test_multiline_hong_kong_stops_before_conference_date(self):
        document = '<p>ICCV 2027 will be held at the <strong>Venue Coming Soon...</strong> in <strong>Hong\n Kong, CN</strong> <strong>October 2–8, 2027</strong>.</p>'
        self.assertEqual(refresh.extract_location(document, 2027)["display"], "Hong Kong, CN")

    def test_placeholder_and_demonstrated_old_truncation_repaired_immediately(self):
        placeholder = {"location": {"display": "Venue information will be posted here"}}
        location, pending = refresh.apply_location_stability(refresh.location_result("California", "location_badge", "California"), placeholder, NOW, HOME_URL)
        self.assertEqual(location["display"], "California")
        self.assertIsNone(pending)
        prior = {"location": {"display": "Hong", "method": "held_in_sentence", "evidence": "ICCV 2027 will be held in Hong", "source_url": HOME_URL}}
        extracted = refresh.location_result("Hong Kong, CN", "held_in_sentence", "ICCV 2027 will be held in Hong Kong, CN October 2–8, 2027")
        location, pending = refresh.apply_location_stability(extracted, prior, NOW, HOME_URL)
        self.assertEqual(location["display"], "Hong Kong, CN")
        self.assertIsNone(pending)

    def test_real_location_change_still_requires_two_observations(self):
        previous = refresh.location_result("Seattle WA", "location_badge", "Seattle WA")
        extracted = refresh.location_result("Vancouver", "location_badge", "Vancouver")
        location, pending = refresh.apply_location_stability(extracted, {"location": previous}, NOW, HOME_URL)
        self.assertEqual(location["display"], "Seattle WA")
        location, pending = refresh.apply_location_stability(extracted, {"location": location, "pending_location": pending}, NOW, HOME_URL)
        self.assertEqual(location["display"], "Vancouver")
        self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()
