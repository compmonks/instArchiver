"""Offline archive regressions: fake HTTP, real temporary archive/state files."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ig_archive as archive


def media(media_id="new", **changes):
    return dict(id=media_id, timestamp="2026-10-20T00:00:00Z",
                media_type="IMAGE", media_url="https://example.invalid/image.jpg", **changes)


class ArchiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.old_state = {"last_saved_media_id": "old", "processed_ids": ["old"],
                          "last_run_iso": "before"}
        self.state_path = self.root / archive.STATE_FILENAME
        self.state_path.write_text(json.dumps(self.old_state), encoding="utf-8")

    def run_archive(self, pages, max_pages=None, since_last=True):
        with patch.object(archive, "fetch_media_page", side_effect=pages) as fetch:
            archive.archive("user", "dummy-token", self.root, 50, max_pages, since_last)
        return fetch

    def test_failed_parent_download_preserves_state_and_can_retry(self):
        page = {"data": [media()]}
        before = self.state_path.read_bytes()
        with patch.object(archive, "download_file", return_value=None):
            with self.assertRaises(archive.ArchiveError):
                self.run_archive([page])
        self.assertEqual(self.state_path.read_bytes(), before)
        with patch.object(archive, "download_file", return_value=self.root / "image.jpg"):
            self.run_archive([page])
        self.assertEqual(archive.load_state(self.root)["processed_ids"], ["new", "old"])

    def test_failed_child_download_preserves_state(self):
        item = media()
        item.update(media_type="CAROUSEL_ALBUM", children={"data": [media("child")]})
        with patch.object(archive, "download_file", side_effect=[self.root / "parent.jpg", None]):
            with self.assertRaises(archive.ArchiveError):
                self.run_archive([{"data": [item]}])
        self.assertEqual(archive.load_state(self.root), self.old_state)

    def test_child_without_url_is_not_silently_skipped(self):
        with self.assertRaises(archive.ArchiveError):
            archive.archive_children(self.root, [{"id": "child"}])

    def test_missing_timestamp_or_url_cannot_be_recorded_complete(self):
        for field in ("timestamp", "media_url"):
            with self.subTest(field=field):
                item = media()
                item.pop(field)
                with self.assertRaises(archive.ArchiveError):
                    self.run_archive([{"data": [item]}])
                self.assertEqual(archive.load_state(self.root), self.old_state)

    def test_empty_carousel_cannot_be_recorded_complete(self):
        item = media()
        item.update(media_type="CAROUSEL_ALBUM")
        item.pop("media_url")
        with patch.object(archive, "fetch_carousel_children", return_value=[]):
            with self.assertRaises(archive.ArchiveError):
                self.run_archive([{"data": [item]}])
        self.assertEqual(archive.load_state(self.root), self.old_state)

    def test_paginated_embedded_children_fetch_full_edge(self):
        item = media()
        item.update(media_type="CAROUSEL_ALBUM", children={
            "data": [media("child1")], "paging": {"next": "https://example.invalid/next"}})
        children = [media("child1"), media("child2")]
        with patch.object(archive, "fetch_carousel_children", return_value=children) as fetch, \
             patch.object(archive, "download_file", return_value=self.root / "media.jpg") as download:
            self.run_archive([{"data": [item]}])
        fetch.assert_called_once_with("new", "dummy-token")
        self.assertEqual(download.call_count, 3)
        stored = json.loads((self.root / "2026-10-20" / "new" / "meta.json").read_text())
        self.assertEqual(stored["children"]["data"], children)
        self.assertNotIn("paging", stored["children"])

    def test_children_edge_pagination_is_exhausted(self):
        with patch.object(archive, "fetch_media_page", side_effect=[
            {"data": [media("a")], "paging": {"next": "https://example.invalid/next"}},
            {"data": [media("b")]},
        ]) as fetch:
            result = archive.fetch_carousel_children("parent", "dummy-token")
        self.assertEqual([child["id"] for child in result], ["a", "b"])
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNone(fetch.call_args.args[1])

    def test_page_cap_keeps_marker_until_unvisited_history_is_processed(self):
        first = {"data": [media()], "paging": {"next": "https://example.invalid/next"}}
        with patch.object(archive, "download_file", return_value=self.root / "media.jpg"):
            self.run_archive([first], max_pages=1)
            capped = archive.load_state(self.root)
            self.assertEqual(capped["last_saved_media_id"], "old")
            self.assertIn("new", capped["processed_ids"])
            self.run_archive([first, {"data": [media("middle"), media("old")]}])
        final = archive.load_state(self.root)
        self.assertEqual(final["last_saved_media_id"], "new")
        self.assertIn("middle", final["processed_ids"])

    def test_since_last_stops_before_skipping_processed_marker(self):
        with patch.object(archive, "archive_media_item") as save:
            fetch = self.run_archive([{"data": [media("old"), media("older")],
                                       "paging": {"next": "https://example.invalid/next"}}])
        save.assert_not_called()
        self.assertEqual(fetch.call_count, 1)

    def test_empty_final_page_advances_completed_marker(self):
        first = {"data": [media()], "paging": {"next": "https://example.invalid/next"}}
        with patch.object(archive, "download_file", return_value=self.root / "media.jpg"):
            self.run_archive([first, {"data": []}])
        self.assertEqual(archive.load_state(self.root)["last_saved_media_id"], "new")

    def test_empty_intermediate_page_follows_next_cursor(self):
        pages = [
            {"data": [media()], "paging": {"next": "https://example.invalid/empty"}},
            {"data": [], "paging": {"next": "https://example.invalid/older"}},
            {"data": [media("middle"), media("old")]},
        ]
        with patch.object(archive, "download_file", return_value=self.root / "image.jpg"):
            fetch = self.run_archive(pages)
        self.assertEqual(fetch.call_count, 3)
        state = archive.load_state(self.root)
        self.assertIn("middle", state["processed_ids"])
        self.assertEqual(state["last_saved_media_id"], "new")

    def test_page_cap_counts_empty_pages_and_preserves_old_marker(self):
        pages = [
            {"data": [media()], "paging": {"next": "https://example.invalid/empty"}},
            {"data": [], "paging": {"next": "https://example.invalid/older"}},
        ]
        with patch.object(archive, "download_file", return_value=self.root / "image.jpg"):
            fetch = self.run_archive(pages, max_pages=2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(archive.load_state(self.root)["last_saved_media_id"], "old")

    def test_state_replace_failure_preserves_old_file_and_cleans_temp(self):
        state = dict(self.old_state, processed_ids=["new", "old"])
        before = self.state_path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                archive.save_state(self.root, state)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(state["last_run_iso"], "before")
        self.assertEqual(list(self.root.glob(".state-*.tmp")), [])

    def test_successful_state_write_is_valid_and_leaves_no_temp(self):
        state = dict(self.old_state)
        archive.save_state(self.root, state)
        self.assertEqual(json.loads(self.state_path.read_text()), state)
        self.assertNotEqual(state["last_run_iso"], "before")
        self.assertEqual(list(self.root.glob(".state-*.tmp")), [])

    def test_partial_http_stream_is_never_published_and_retry_can_succeed(self):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def interrupted_stream(**_kwargs):
            yield b"partial"
            raise archive.requests.ConnectionError("simulated interruption")
        response.iter_content.side_effect = interrupted_stream
        dest = self.root / "download.jpg"
        with patch.object(archive.requests, "get", return_value=response):
            self.assertIsNone(archive.download_file("https://example.invalid/image.jpg", dest,
                                                  max_attempts=1))
            self.assertFalse(dest.exists())
            response.iter_content.side_effect = None
            response.iter_content.return_value = [b"complete-image"]
            self.assertEqual(archive.download_file("https://example.invalid/image.jpg", dest), dest)
        self.assertEqual(dest.read_bytes(), b"complete-image")

    def test_empty_http_body_is_not_published(self):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = []
        dest = self.root / "empty.jpg"
        with patch.object(archive.requests, "get", return_value=response) as get, \
             patch.object(archive.time, "sleep"):
            self.assertIsNone(archive.download_file("https://example.invalid/empty.jpg", dest,
                                                  max_attempts=2))
        self.assertEqual(get.call_count, 2)
        self.assertFalse(dest.exists())

    def test_empty_response_then_success_uses_download_retry_budget(self):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.side_effect = [[], [b"complete-image"]]
        dest = self.root / "retry.jpg"
        with patch.object(archive.requests, "get", return_value=response) as get, \
             patch.object(archive.time, "sleep"):
            result = archive.download_file("https://example.invalid/retry.jpg", dest,
                                           max_attempts=2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result, dest)
        self.assertEqual(dest.read_bytes(), b"complete-image")

    def test_recovery_reuses_inferred_extension_without_http(self):
        dest = self.root / "media_01"
        completed = dest.with_suffix(".jpg")
        completed.write_bytes(b"previously-completed")
        dest.with_suffix(".png.part").write_bytes(b"partial")
        with patch.object(archive.requests, "get") as get:
            result = archive.download_file("https://example.invalid/expired-url", dest)
        get.assert_not_called()
        self.assertEqual(result, completed)
        self.assertEqual(completed.read_bytes(), b"previously-completed")

    def test_recovery_refuses_ambiguous_completed_files(self):
        dest = self.root / "media_01"
        dest.with_suffix(".jpg").write_bytes(b"one")
        dest.with_suffix(".png").write_bytes(b"two")
        with patch.object(archive.requests, "get") as get:
            with self.assertRaises(archive.ArchiveError):
                archive.download_file("https://example.invalid/expired-url", dest)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
