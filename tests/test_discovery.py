"""Offline discovery boundaries: changing annual hosts need an official chain."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_official as refresh


NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)
SOURCE = {
    "slug": "cvpr", "title": "CVPR", "aliases": ["CVPR"],
    "description": "Computer Vision and Pattern Recognition", "rank": "A",
    "year_rule": "annual_next", "series_url": "https://society.example/conferences",
    "trusted_hosts": ["society.example"], "url": "https://cvpr{year}.old-pattern.example/",
}
NEW = "https://renamed-conference.example/"
OLD = "https://previous-conference.example/2026/"


def page(url, document, final_url=None):
    return refresh.Page(url, final_url or url, document, 200)


def home(year=2027, content=""):
    return f"<title>CVPR {year}</title><h1>CVPR {year}</h1><p>Welcome to the annual conference.</p>{content}"


def verified_prior(year=2026, url=OLD):
    return {"year": year, "official_url": url, "verified_at": "2026-01-02T00:00:00+00:00",
            "discovered_from": SOURCE["series_url"], "discovery_method": "official_hub_link",
            "timezone": "UTC-12", "deadlines": {"deadline": "2026-01-01 23:59:59"},
            "location": {"display": "Old city", "method": "fixture"}}


class DiscoveryTests(unittest.TestCase):
    def run_refresh(self, documents, prior=None, results=None, diagnostics=None):
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            if url not in documents:
                raise RuntimeError("offline fixture: unavailable")
            result = documents[url]
            return result if isinstance(result, refresh.Page) else page(url, result)

        search_result = (results or [], diagnostics or [{"provider": "fixture", "status": "empty"}])
        with patch.object(refresh, "fetch", side_effect=fake_fetch), \
             patch.object(refresh, "discover_from_sitemaps", return_value=[]), \
             patch.object(refresh, "search_web", return_value=search_result) as search:
            venue, state = refresh.refresh_source(SOURCE, prior or {}, NOW)
        return venue, state, calls, search

    def test_new_opaque_domain_discovered_from_previous_edition_after_rollover(self):
        prior = verified_prior()
        documents = {
            SOURCE["series_url"]: "<h1>Conference series</h1>",
            OLD: home(2026, f'<p>CVPR 2027 will be hosted at a new address. Visit the <a href="{NEW}">website</a>.</p>'),
            NEW: home(),
        }
        venue, state, _, search = self.run_refresh(documents, prior)
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "previous_edition_link")
        self.assertEqual(state["provenance"][-1]["url"], OLD)
        self.assertEqual(state["provenance"][-1]["target_url"], NEW)
        self.assertIn("CVPR 2027", state["provenance"][-1]["evidence"])
        self.assertEqual(state["deadlines"], {})
        self.assertIsNone(state["location"])
        self.assertEqual(venue["confs"][0]["place"], "TBD")
        self.assertEqual([item["year"] for item in state["official_history"]], [2027, 2026])
        self.assertEqual(search.call_count, 2)

    def test_history_survives_unresolved_year_and_remains_a_discovery_seed(self):
        prior = {"year": 2027, "official_url": None, "official_history": [verified_prior()]}
        _, state, calls, _ = self.run_refresh({
            OLD: home(2026, f'<a href="{NEW}">CVPR 2027</a>'), NEW: home(),
        }, prior)
        self.assertIn(OLD, calls)
        self.assertEqual(state["official_url"], NEW)

    def test_previous_opaque_site_can_roll_its_own_root_to_the_next_edition(self):
        prior = verified_prior(url=NEW)
        _, state, _, _ = self.run_refresh({NEW: home()}, prior)
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "previous_edition_update")

    def test_verified_previous_homepage_can_redirect_to_a_new_official_domain(self):
        redirected = refresh.Page(OLD, NEW, home(), 200, ("https://migration.example/", NEW))
        _, state, _, _ = self.run_refresh({OLD: redirected, NEW: home()}, verified_prior())
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "previous_edition_redirect")
        self.assertEqual(state["provenance"][-1]["redirects"], ["https://migration.example/", NEW])

    def test_previous_edition_redirect_can_change_year_path_on_the_same_host(self):
        new_path = "https://previous-conference.example/2027/"
        redirected = refresh.Page(OLD, new_path, home(), 200, (new_path,))
        _, state, _, _ = self.run_refresh({OLD: redirected, new_path: home()}, verified_prior())
        self.assertEqual(state["official_url"], new_path)
        self.assertEqual(state["discovery_method"], "previous_edition_redirect")
        self.assertEqual(state["provenance"][-1]["redirects"], [new_path])

    def test_newly_corroborated_site_supersedes_still_accessible_cached_same_year_site(self):
        cached = "https://cached-conference.example/"
        _, state, _, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{NEW}">CVPR 2027 official website</a>',
            cached: home(), NEW: home(),
        }, verified_prior(2027, cached))
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "official_hub_link")

    def test_daily_search_finds_replacement_while_cached_homepage_still_works(self):
        cached = "https://cached-conference.example/"
        announcement = "https://society.example/news/cvpr-2027"
        _, state, _, search = self.run_refresh({
            SOURCE["series_url"]: "<h1>Conference series</h1>",
            cached: home(), NEW: home(),
            announcement: f'<h1>CVPR 2027 news</h1><p>CVPR 2027 official website: <a href="{NEW}">visit here</a>.</p>',
        }, verified_prior(2027, cached), results=[announcement],
            diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "official_search_link")
        self.assertEqual(state["provenance"][0]["method"], "trusted_organizer_search")

    def test_search_listing_does_not_replace_a_working_dedicated_homepage(self):
        listing = "https://society.example/event/cvpr-2027"
        _, state, _, search = self.run_refresh({NEW: home(), listing: home()},
            verified_prior(2027, NEW), results=[listing], diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(state["official_url"], NEW)

    def test_fresh_proof_is_kept_when_search_rediscovers_cached_url(self):
        announcement = "https://society.example/news/cvpr-2027"
        _, state, _, _ = self.run_refresh({
            NEW: home(), announcement: f'<p>CVPR 2027 <a href="{NEW}">official website</a></p>',
        }, verified_prior(2027, NEW), results=[announcement])
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_method"], "official_search_link")

    def test_tourism_link_with_edition_in_url_does_not_replace_official_homepage(self):
        tourism = "https://tourism.example/cvpr-2027/"
        _, state, calls, _ = self.run_refresh({
            NEW: home(content=f'<a href="{tourism}">Visit Toronto</a>'), tourism: home(),
        }, verified_prior(2027, NEW))
        self.assertEqual(state["official_url"], NEW)
        self.assertNotIn(tourism, calls)

    def test_edition_path_precedes_its_yearless_hub_self_link(self):
        source = {**SOURCE, "url": "https://society.example/Conferences/{year}",
                  "series_url": "https://society.example/"}
        with patch.dict(SOURCE, source):
            official = refresh.formatted_candidate(SOURCE, 2027)
            _, state, _, _ = self.run_refresh({
                SOURCE["series_url"]: home(content='<a href="/">CVPR 2027</a>'), official: home(),
            }, verified_prior(2027, official))
        self.assertEqual(state["official_url"], official)
        self.assertEqual(state["discovery_method"], "last_verified")

    def test_author_policy_subpage_cannot_be_promoted_to_edition_homepage(self):
        policy = "https://society.example/cvpr-2027/policies-for-authors/"
        _, state, calls, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{policy}">CVPR 2027 author policies</a>',
            policy: '<h1>CVPR 2027 Policies for Authors</h1><p>Conference policy information.</p>',
        }, results=[policy])
        self.assertIsNone(state["official_url"])

    def test_pricing_link_in_local_navigation_cannot_replace_canonical_homepage(self):
        source = {**SOURCE, "url": "https://society.example/Conferences/{year}",
                  "series_url": "https://society.example/"}
        with patch.dict(SOURCE, source):
            official = refresh.formatted_candidate(SOURCE, 2027)
            pricing = official + "/Pricing"
            _, state, calls, _ = self.run_refresh({
                SOURCE["series_url"]: home(content=f'<p>CVPR 2027 conference <a href="{pricing}">Pricing</a></p>'),
                official: home(), pricing: home(),
            })
        self.assertEqual(state["official_url"], official)
        self.assertNotIn(pricing, calls)

    def test_ordinary_internal_child_does_not_outrank_canonical_homepage(self):
        source = {**SOURCE, "url": "https://society.example/Conferences/{year}",
                  "series_url": "https://society.example/"}
        with patch.dict(SOURCE, source):
            official = refresh.formatted_candidate(SOURCE, 2027)
            child = official + "/Attend"
            _, state, calls, _ = self.run_refresh({
                SOURCE["series_url"]: home(content=f'<p>CVPR 2027 conference <a href="{child}">Attend</a></p>'),
                official: home(), child: home(),
            })
        self.assertEqual(state["official_url"], official)
        self.assertNotIn(child, calls)

    def test_explicit_new_homepage_endorsement_can_move_into_child_path(self):
        source = {**SOURCE, "url": "https://society.example/Conferences/{year}",
                  "series_url": "https://society.example/"}
        with patch.dict(SOURCE, source):
            official = refresh.formatted_candidate(SOURCE, 2027)
            replacement = official + "/new-site"
            _, state, _, _ = self.run_refresh({
                SOURCE["series_url"]: home(content=f'<p>CVPR 2027 has moved. <a href="{replacement}">Official website</a></p>'),
                official: home(), replacement: home(),
            })
        self.assertEqual(state["official_url"], replacement)

    def test_ordinary_child_cannot_replace_a_learned_opaque_homepage(self):
        child = NEW + "Attend"
        for context in ("CVPR 2027 conference", "CVPR 2027 official website"):
            with self.subTest(context=context):
                _, state, calls, _ = self.run_refresh({
                    NEW: home(content=f'<p>{context} <a href="{child}">Attend</a></p>'), child: home(),
                }, verified_prior(2027, NEW))
                self.assertEqual(state["official_url"], NEW)
                self.assertNotIn(child, calls)

    def test_learned_homepage_can_explicitly_endorse_a_new_child_homepage(self):
        child = NEW + "new-site"
        _, state, _, _ = self.run_refresh({
            NEW: home(content=f'<p>CVPR 2027 has moved. <a href="{child}">Official website</a></p>'), child: home(),
        }, verified_prior(2027, NEW))
        self.assertEqual(state["official_url"], child)

    def test_learned_homepage_does_not_revert_to_old_pattern_when_search_is_blocked(self):
        with patch.dict(SOURCE, {"trusted_hosts": ["society.example", "old-pattern.example"]}):
            old_pattern = refresh.formatted_candidate(SOURCE, 2027)
            _, state, _, _ = self.run_refresh({NEW: home(), old_pattern: home()},
                verified_prior(2027, NEW), diagnostics=[{"provider": "fixture", "status": "blocked", "error": "CAPTCHA"}])
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovery_details"]["search_status"], "unavailable")

    def test_blog_category_in_announcement_cannot_outrank_canonical_home(self):
        source = {**SOURCE, "url": "https://society.example/Conferences/{year}",
                  "series_url": "https://society.example/"}
        announcement = "https://blog.society.example/news/submission-policies-for-cvpr-2027/"
        category = "https://blog.society.example/category/cvpr-2027/"
        with patch.dict(SOURCE, source):
            official = refresh.formatted_candidate(SOURCE, 2027)
            documents = {
                SOURCE["series_url"]: f'<a href="{announcement}">CVPR 2027 News</a>',
                announcement: f'<h1>CVPR 2027 policies</h1><a href="{category}">CVPR 2027</a>',
                official: home(), category: home(),
            }
            _, state, calls, _ = self.run_refresh(documents)
            self.assertEqual(state["official_url"], official)
            self.assertNotIn(category, calls)
            # A temporary outage must not turn an ancillary page into durable
            # homepage state that then wins after the real site recovers.
            documents.pop(official)
            prior = verified_prior(2027, official)
            _, retained, calls, _ = self.run_refresh(documents, prior)
            self.assertEqual(retained["official_url"], official)
            self.assertEqual(retained["status"], "retained")
            self.assertEqual(retained["verified_at"], prior["verified_at"])
            self.assertNotIn(category, calls)
            self.assertTrue(any(item["url"] == category for item in retained["unverified_candidates"]))
            documents[official] = home()
            _, restored, calls, _ = self.run_refresh(documents, retained)
            self.assertEqual(restored["official_url"], official)
            self.assertEqual(restored["status"], "verified")
            self.assertNotIn(category, calls)

    def test_weak_category_is_unverified_without_a_known_homepage(self):
        category = "https://blog.society.example/category/cvpr-2027/"
        _, state, calls, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{category}">CVPR 2027</a>', category: home(),
        }, results=[category])
        self.assertIsNone(state["official_url"])
        self.assertEqual(state["official_history"], [])
        self.assertTrue(any(item["url"] == category for item in state["unverified_candidates"]))

    def test_weak_duplicate_keeps_learned_canonical_provenance(self):
        learned = "https://renamed-conference.example/custom/location/"
        prior = verified_prior(2027, learned)
        prior["provenance"] = [{"url": SOURCE["series_url"], "method": "official_hub_link",
                                "target_url": learned, "evidence": "CVPR 2027 official website"}]
        _, state, _, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{learned}">CVPR 2027</a>', learned: home(),
        }, prior)
        self.assertEqual(state["official_url"], learned)
        self.assertEqual(state["discovery_method"], "last_verified")
        self.assertEqual(state["provenance"], prior["provenance"])

    def test_learned_homepage_survives_cross_host_ancillary_edition_link(self):
        category = "https://blog.society.example/category/cvpr-2027/"
        _, state, calls, _ = self.run_refresh({
            NEW: home(content=f'<a href="{category}">CVPR 2027</a>'), category: home(),
        }, verified_prior(2027, NEW))
        self.assertEqual(state["official_url"], NEW)
        self.assertNotIn(category, calls)

    def test_explicit_nested_homepage_context_still_supersedes_canonical_home(self):
        replacement = "https://new-conference.example/2027/main/"
        _, state, _, _ = self.run_refresh({
            NEW: home(content=f'<p>CVPR 2027 new official website: <a href="{replacement}">click here</a></p>'),
            replacement: home(),
        }, verified_prior(2027, NEW))
        self.assertEqual(state["official_url"], replacement)

    def test_dedicated_pattern_beats_organizer_listing_but_listing_beats_weak_blog(self):
        listing = "https://society.example/cvpr-2027"
        category = "https://blog.society.example/category/cvpr-2027/"
        with patch.dict(SOURCE, {"trusted_hosts": ["society.example", "old-pattern.example"]}):
            official = refresh.formatted_candidate(SOURCE, 2027)
            documents = {
                SOURCE["series_url"]: f'<a href="{listing}">CVPR 2027 official website</a><a href="{category}">CVPR 2027</a>',
                official: home(), listing: home(), category: home(),
            }
            _, state, _, _ = self.run_refresh(documents)
            self.assertEqual(state["official_url"], official)
            documents.pop(official)
            _, fallback, _, _ = self.run_refresh(documents)
            self.assertEqual(fallback["official_url"], listing)

    def test_submission_platforms_cannot_become_homepages_from_links_or_search(self):
        portals = (
            "https://openreview.net/group?id=cvpr/2027/Conference",
            "https://cmt3.research.microsoft.com/CVPR2027/",
            "https://easychair.org/conferences/?conf=cvpr2027",
            "https://edas.info/N12345",
        )
        for portal in portals:
            with self.subTest(portal=portal):
                _, state, calls, _ = self.run_refresh({
                    SOURCE["series_url"]: f'<p>CVPR 2027 official website <a href="{portal}">{portal}</a></p>',
                    portal: home(), NEW: home(),
                }, verified_prior(2027, NEW), results=[portal])
                self.assertEqual(state["official_url"], NEW)
                self.assertNotIn(portal, calls)
                self.assertFalse(refresh.probable_homepage_reference(portal, "CVPR 2027 official website", SOURCE, 2027))
                candidate = refresh.Candidate(portal, SOURCE["series_url"], "official_hub_link")
                with patch.object(refresh, "fetch", return_value=page(portal, home())) as fetch:
                    result, _ = refresh.verify_candidate(candidate, SOURCE, 2027)
                self.assertIsNone(result)
                fetch.assert_not_called()

    def test_official_seed_redirect_to_submission_portal_cannot_promote_it(self):
        portal = "https://openreview.net/group?id=cvpr/2027/Conference"
        redirected = refresh.Page(OLD, portal, home(), 200, (portal,))
        _, state, calls, _ = self.run_refresh({OLD: redirected, portal: home()}, verified_prior())
        self.assertIsNone(state["official_url"])
        self.assertNotIn(portal, calls)

    def test_without_history_verified_series_old_edition_can_bootstrap_new_domain(self):
        _, state, calls, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{OLD}">CVPR 2026</a>',
            OLD: home(2026, f'<p>CVPR 2027: <a href="{NEW}">official website</a></p>'),
            NEW: home(),
        })
        self.assertEqual(state["official_url"], NEW)
        self.assertIn(OLD, calls)
        self.assertEqual([item["url"] for item in state["provenance"]], [SOURCE["series_url"], OLD])

    def test_one_hop_news_page_corroborates_an_opaque_external_homepage(self):
        announcement = "https://society.example/news/new-host"
        venue, state, calls, search = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{announcement}">Future conferences</a>',
            announcement: f'<h1>Announcement</h1><p>CVPR 2027 official website: <a href="{NEW}">visit here</a>.</p>',
            NEW: home(),
        })
        self.assertEqual(venue["confs"][0]["discovery_status"], "verified")
        self.assertEqual(state["discovered_from"], announcement)
        self.assertEqual([item["url"] for item in state["provenance"]], [SOURCE["series_url"], announcement])
        self.assertIn(announcement, calls)
        self.assertEqual(search.call_count, 2)

    def test_search_candidate_without_authoritative_reference_is_never_fetched(self):
        venue, state, calls, search = self.run_refresh({NEW: home()}, results=[NEW],
            diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertIsNone(state["official_url"])
        self.assertNotIn(NEW, calls)
        self.assertEqual(venue["confs"][0]["discovery_status"], "unverified_candidates")
        self.assertEqual(venue["confs"][0]["place_status"], "not_detected")
        self.assertEqual(state["unverified_candidates"][0]["url"], NEW)
        self.assertEqual(search.call_count, 2)

    def test_search_trusted_organizer_announcement_supplies_official_proof(self):
        announcement = "https://society.example/news/cvpr-2027"
        _, state, _, _ = self.run_refresh({
            announcement: f'<h1>CVPR 2027 news</h1><p>CVPR 2027: <a href="{NEW}">Official website</a></p>',
            NEW: home(),
        }, results=[NEW, announcement], diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["discovered_from"], announcement)
        self.assertEqual(state["provenance"][0]["method"], "trusted_organizer_search")
        self.assertEqual(state["discovery_details"]["search_status"], "completed")
        self.assertEqual(state["unverified_candidates"], [])

    def test_search_result_under_revalidated_previous_edition_can_corroborate_new_host(self):
        announcement = OLD + "news/next-edition"
        _, state, calls, search = self.run_refresh({
            OLD: home(2026),
            announcement: f'<h1>CVPR 2027 news</h1><p>CVPR 2027: <a href="{NEW}">Official website</a></p>',
            NEW: home(),
        }, verified_prior(), results=[announcement], diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertEqual(state["official_url"], NEW)
        self.assertIn(announcement, calls)
        self.assertEqual(state["provenance"][0]["method"], "previous_edition_search")
        self.assertIn("site:previous-conference.example", search.call_args_list[-1].args[0])

    def test_search_result_outside_previous_edition_path_is_unverified(self):
        announcement = "https://previous-conference.example/unrelated/news"
        _, state, calls, _ = self.run_refresh({
            OLD: home(2026), announcement: f'<a href="{NEW}">CVPR 2027</a>', NEW: home(),
        }, verified_prior(), results=[announcement], diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertIsNone(state["official_url"])
        self.assertNotIn(announcement, calls)

    def test_encoded_traversal_search_result_cannot_inherit_official_authority(self):
        for escape in ("%2e%2e", "%252e%252e", "%2e%2e%2f", "%252e%252e%252f", "%2e%2e%5c"):
            with self.subTest(escape=escape):
                announcement = OLD + escape + "/unrelated/cvpr2027-news"
                _, state, calls, _ = self.run_refresh({
                    OLD: home(2026), announcement: f'<a href="{NEW}">CVPR 2027 official website</a>',
                    NEW: home(),
                }, verified_prior(), results=[announcement])
                self.assertIsNone(state["official_url"])
                self.assertNotIn(announcement, calls)
                self.assertNotIn(NEW, calls)

    def test_ambiguous_homepage_and_deadline_paths_cannot_bypass_scope_with_identity(self):
        for escape in ("%2e%2e", "%252e%252e", "%2e%2e%2f", "%252e%252e%255c"):
            with self.subTest(escape=escape):
                unsafe = NEW + "2027/" + escape + "/cvpr2027/important-dates"
                self.assertFalse(refresh.deadline_reference(unsafe, "CVPR 2027 Important Dates", page(NEW, home()), SOURCE, 2027))
                candidate = refresh.Candidate(unsafe, SOURCE["series_url"], "official_hub_link")
                with patch.object(refresh, "fetch", return_value=page(unsafe, home())) as fetch:
                    result, _ = refresh.verify_candidate(candidate, SOURCE, 2027)
                self.assertIsNone(result)
                fetch.assert_not_called()

    def test_verified_seed_redirect_cannot_promote_an_ambiguous_target(self):
        unsafe = OLD + "%252e%252e/cvpr2027/"
        redirected = refresh.Page(OLD, unsafe, home(), 200, (unsafe,))
        _, state, _, _ = self.run_refresh({OLD: redirected, unsafe: home()}, verified_prior())
        self.assertIsNone(state["official_url"])

    def test_blocked_search_retains_same_year_verified_facts_and_reports_failure(self):
        prior = verified_prior(2027, NEW)
        venue, state, _, _ = self.run_refresh({}, prior,
            diagnostics=[{"provider": "fixture", "status": "blocked", "error": "CAPTCHA"}])
        self.assertEqual(state["deadlines"], prior["deadlines"])
        self.assertEqual(state["location"], prior["location"])
        self.assertEqual(state["official_url"], NEW)
        self.assertEqual(state["verified_at"], prior["verified_at"])
        self.assertEqual(venue["confs"][0]["discovery_status"], "retained")
        self.assertEqual(state["discovery_details"]["search_status"], "unavailable")
        self.assertIn("CAPTCHA", state["error"])

    def test_blocked_search_with_no_history_is_distinct_from_not_announced(self):
        venue, state, _, _ = self.run_refresh({}, diagnostics=[{"provider": "fixture", "status": "error", "error": "network down"}])
        self.assertEqual(venue["confs"][0]["discovery_status"], "search_unavailable")
        self.assertFalse(venue["confs"][0]["official_page_announced"])

    def test_later_check_reads_dates_from_verified_opaque_homepage(self):
        dates = NEW + "important-dates/"
        prior = verified_prior(2027, NEW)
        _, state, calls, search = self.run_refresh({
            NEW: home(content='<a href="/important-dates/">Important Dates</a>'),
            dates: '<h1>Important Dates</h1><p>Full Paper Submission Deadline: September 25, 2026 AoE</p>',
        }, prior)
        self.assertIn(dates, calls)
        self.assertEqual(state["deadlines"]["deadline"], "2026-09-25 23:59:59")
        self.assertEqual(state["deadline_evidence"]["deadline"]["source_url"], dates)
        self.assertEqual(search.call_count, 2)

    def test_uncorroborated_redirect_and_previous_year_homepage_are_rejected(self):
        for target in (page(NEW, home(), "https://unlinked.example/"), home(2026)):
            with self.subTest(target=target):
                _, state, _, _ = self.run_refresh({
                    SOURCE["series_url"]: f'<a href="{NEW}">CVPR 2027</a>', NEW: target,
                })
                self.assertIsNone(state["official_url"])
                self.assertTrue(state["unverified_candidates"])

    def test_search_organizer_redirect_cannot_grant_an_untrusted_site_authority(self):
        announcement = "https://society.example/news/cvpr-2027"
        _, state, calls, _ = self.run_refresh({
            announcement: page(announcement, f'<a href="{NEW}">CVPR 2027</a>', "https://untrusted.example/"),
            NEW: home(),
        }, results=[announcement], diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertIsNone(state["official_url"])
        self.assertNotIn(NEW, calls)

    def test_prior_edition_host_does_not_authorize_unrelated_paths(self):
        outside = "https://previous-conference.example/unrelated/news"
        _, state, calls, _ = self.run_refresh({
            OLD: home(2026, f'<a href="{outside}">CVPR 2027 News</a>'),
            outside: f'<a href="{NEW}">CVPR 2027</a>', NEW: home(),
        }, verified_prior())
        self.assertNotIn(outside, calls)
        self.assertIsNone(state["official_url"])

    def test_reviewer_nomination_form_cannot_replace_a_conference_homepage(self):
        form = "https://docs.google.com/forms/d/e/nomination/viewform"
        venue, state, calls, _ = self.run_refresh({
            SOURCE["series_url"]: f'<a href="{form}">CVPR 2027 Reviewer Self-Nomination Form</a>',
            NEW: home(), form: home(),
        }, verified_prior(2027, NEW))
        self.assertEqual(state["official_url"], NEW)
        self.assertNotIn(form, calls)
        self.assertTrue(refresh.submission_portal("https://forms.gle/example"))
        self.assertFalse(refresh.submission_portal("https://sites.google.com/view/cvpr2027"))

    def test_excluded_cached_portal_is_not_retained_as_homepage_on_failure(self):
        portal = "https://openreview.net/group?id=CVPR/2027/Conference"
        prior = verified_prior(2027, portal)
        prior["deadline_evidence"] = {"deadline": {"source_url": portal, "status": "verified"}}
        venue, state, _, _ = self.run_refresh({}, prior,
            diagnostics=[{"provider": "fixture", "status": "blocked", "error": "CAPTCHA"}])
        self.assertIsNone(state["official_url"])
        self.assertIsNone(state["verified_at"])
        self.assertEqual(state["official_history"], [])
        self.assertEqual(state["deadlines"], prior["deadlines"])
        self.assertEqual(state["deadline_evidence"]["deadline"]["source_url"], portal)
        self.assertEqual(venue["confs"][0]["link"], SOURCE["series_url"])
        self.assertFalse(venue["confs"][0]["official_page_announced"])

    def test_history_and_search_diagnostics_are_bounded(self):
        prior = {"year": 2027, "official_history": [verified_prior(year, f"https://old-{year}.example/") for year in range(2020, 2027)]}
        _, state, _, search = self.run_refresh({}, prior, results=[f"https://lead-{i}.example/" for i in range(20)],
            diagnostics=[{"provider": "fixture", "status": "ok"}])
        self.assertLessEqual(len(state["official_history"]), refresh.MAX_HISTORY)
        self.assertLessEqual(len(state["unverified_candidates"]), refresh.MAX_UNVERIFIED)
        self.assertLessEqual(search.call_count, 2)

    def test_opaque_link_must_have_local_edition_context(self):
        document = f'<div><p>CVPR 2027 is coming.</p><p>Unrelated service <a href="{NEW}">website</a>.</p></div>'
        found = refresh.linked_homepages(page(SOURCE["series_url"], document), SOURCE, 2027, "official_hub_link")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
