import unittest
from unittest import mock

import transfer


class TestFindRclone(unittest.TestCase):
    def test_found_on_path(self):
        with mock.patch("transfer.shutil.which", return_value="/usr/bin/rclone"):
            self.assertEqual(transfer.find_rclone(), "/usr/bin/rclone")

    def test_falls_back_to_cwd(self):
        with mock.patch("transfer.shutil.which", return_value=None), \
             mock.patch("transfer.Path.is_file", return_value=True):
            found = transfer.find_rclone(cwd=".")
            self.assertIsNotNone(found)
            self.assertIn("rclone", found)

    def test_absent_returns_none(self):
        with mock.patch("transfer.shutil.which", return_value=None), \
             mock.patch("transfer.Path.is_file", return_value=False):
            self.assertIsNone(transfer.find_rclone(cwd="."))


class TestClassifyError(unittest.TestCase):
    def test_network_errors_are_unreachable(self):
        for stderr in ("dial tcp 10.0.0.1:443: i/o timeout",
                       "no such host",
                       "connection refused",
                       "TLS handshake timeout"):
            self.assertEqual(transfer.classify_error(stderr), "unreachable", stderr)

    def test_bad_keys_are_credentials(self):
        for stderr in ("InvalidAccessKeyId: The AWS Access Key Id you provided",
                       "SignatureDoesNotMatch",
                       "401 Unauthorized"):
            self.assertEqual(transfer.classify_error(stderr), "credentials", stderr)

    def test_denied_is_permission(self):
        self.assertEqual(transfer.classify_error("AccessDenied: Access Denied"),
                         "permission")

    def test_missing_is_not_found(self):
        for stderr in ("NoSuchBucket", "404 Not Found",
                       "error reading source directory: directory not found"):
            self.assertEqual(transfer.classify_error(stderr), "not_found", stderr)

    def test_anything_else_is_unknown(self):
        self.assertEqual(transfer.classify_error("segfault or whatever"), "unknown")

    def test_subprocess_timeout_reported_as_unreachable(self):
        import subprocess as sp

        def hang(cmd, stdout=None, stderr=None, timeout=None):
            raise sp.TimeoutExpired(cmd, timeout)

        with mock.patch("transfer.subprocess.run", side_effect=hang):
            code, out, err = transfer._run("rclone", ["lsjson", "x:y"])
        self.assertNotEqual(code, 0)
        self.assertEqual(transfer.classify_error(err), "unreachable")


class TestPreflightChecks(unittest.TestCase):
    def test_remote_names_parsed(self):
        with mock.patch("transfer._run", return_value=(0, "ceph:\nother:\n", "")):
            self.assertEqual(transfer.remote_names("rclone"), ["ceph", "other"])

    def test_check_access_ok(self):
        with mock.patch("transfer._run", return_value=(0, "[]", "")):
            self.assertIsNone(transfer.check_access("rclone", "ceph", "crsw"))

    def test_check_access_classifies_failure(self):
        with mock.patch("transfer._run",
                        return_value=(1, "", "dial tcp: i/o timeout")):
            self.assertEqual(transfer.check_access("rclone", "ceph", "crsw"),
                             "unreachable")

    def test_check_write_probes_and_cleans_up(self):
        calls = []

        def fake_run(rclone, args, timeout=30):
            calls.append(args)
            return (0, "", "")

        with mock.patch("transfer._run", side_effect=fake_run):
            self.assertIsNone(transfer.check_write("rclone", "ceph", "crsw", "rs2/csac"))
        self.assertEqual(calls[0][0], "touch")
        self.assertEqual(calls[1][0], "deletefile")
        self.assertIn("ceph:crsw/rs2/csac/.crsw-preflight-probe", calls[0])

    def test_check_write_denied(self):
        with mock.patch("transfer._run", return_value=(1, "", "AccessDenied")):
            self.assertEqual(
                transfer.check_write("rclone", "ceph", "crsw", "rs3/x"),
                "permission")


class TestCopyto(unittest.TestCase):
    def test_uses_copyto_never_copy(self):
        with mock.patch("transfer._run", return_value=(0, "", "")) as run:
            transfer.copyto("rclone", "local/a.csv", "ceph", "crsw",
                            "rs2/csac/green/2_final/a.csv")
        args = run.call_args[0][1]
        self.assertEqual(args[0], "copyto")  # NEVER plain "copy" (spec §9)
        self.assertIn("ceph:crsw/rs2/csac/green/2_final/a.csv", args)

    def test_failure_raises_classified_error(self):
        with mock.patch("transfer._run", return_value=(1, "", "AccessDenied")):
            with self.assertRaises(transfer.TransferError) as ctx:
                transfer.copyto("rclone", "a.csv", "ceph", "crsw",
                                "rs2/x/green/0_raw/a.csv")
            self.assertEqual(ctx.exception.kind, "permission")


class TestCopytoHeaders(unittest.TestCase):
    HEADERS = {"x-amz-meta-dataset-uuid": "8f14e45f",
               "x-amz-meta-sensitivity": "green"}

    def test_headers_become_upload_flags(self):
        with mock.patch("transfer._run", return_value=(0, "", "")) as run:
            transfer.copyto("rclone", "a.csv", "ceph", "crsw",
                            "rs2/csac/green/2_final/a.csv",
                            headers=self.HEADERS)
        args = run.call_args[0][1]
        self.assertEqual(args[:5], [
            "copyto",
            "--header-upload", "x-amz-meta-dataset-uuid: 8f14e45f",
            "--header-upload", "x-amz-meta-sensitivity: green"])
        self.assertEqual(args[-1], "ceph:crsw/rs2/csac/green/2_final/a.csv")

    def test_no_headers_no_flags(self):
        with mock.patch("transfer._run", return_value=(0, "", "")) as run:
            transfer.copyto("rclone", "a.csv", "ceph", "crsw", "k/a.csv")
        self.assertNotIn("--header-upload", run.call_args[0][1])

    def test_progress_path_carries_headers_too(self):
        with mock.patch("transfer.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            transfer.copyto("rclone", "a.csv", "ceph", "crsw", "k/a.csv",
                            show_progress=True, headers=self.HEADERS)
        argv = run.call_args[0][0]
        self.assertIn("--header-upload", argv)
        self.assertIn("x-amz-meta-dataset-uuid: 8f14e45f", argv)
        self.assertEqual(argv[-1], "ceph:crsw/k/a.csv")


class TestReadKey(unittest.TestCase):
    KEY = "rs2/csac/green/2_final/dataset.meta.json"

    def test_success_returns_text(self):
        with mock.patch("transfer._run", return_value=(0, '{"a": 1}', "")) as run:
            text, err = transfer.read_key("rclone", "ceph", "crsw", self.KEY)
        self.assertEqual((text, err), ('{"a": 1}', None))
        self.assertEqual(run.call_args[0][1][0], "cat")

    def test_missing_object_is_absent(self):
        with mock.patch("transfer._run",
                        return_value=(1, "", "error: object not found")):
            text, err = transfer.read_key("rclone", "ceph", "crsw", self.KEY)
        self.assertEqual((text, err), (None, "absent"))

    def test_permission_failure_is_not_absence(self):
        # Building on an unread record would silently drop its members.
        with mock.patch("transfer._run", return_value=(1, "", "AccessDenied")):
            text, err = transfer.read_key("rclone", "ceph", "crsw", self.KEY)
        self.assertEqual((text, err), (None, "permission"))

    def test_network_failure_is_not_absence(self):
        with mock.patch("transfer._run",
                        return_value=(1, "", "dial tcp: i/o timeout")):
            self.assertEqual(
                transfer.read_key("rclone", "ceph", "crsw", self.KEY),
                (None, "unreachable"))


class TestReadMetadata(unittest.TestCase):
    def test_returns_label_dict(self):
        listing = ('[{"Name":"a.csv","Size":10,"Metadata":'
                   '{"dataset-uuid":"8f14e45f","sensitivity":"green"}}]')
        with mock.patch("transfer._run", return_value=(0, listing, "")) as run:
            metadata = transfer.read_metadata("rclone", "ceph", "crsw", "k/a.csv")
        self.assertEqual(metadata["sensitivity"], "green")
        self.assertIn("--metadata", run.call_args[0][1])

    def test_missing_or_failed_is_none(self):
        for result in ((1, "", "not found"), (0, "[]", ""),
                       (0, "not json", ""), (0, '[{"Name":"a.csv"}]', "")):
            with mock.patch("transfer._run", return_value=result):
                self.assertIsNone(
                    transfer.read_metadata("rclone", "ceph", "crsw", "k"),
                    repr(result))


class TestKeyExists(unittest.TestCase):
    def test_existing_key(self):
        listing = '[{"Path":"a.csv","Name":"a.csv","Size":10}]'
        with mock.patch("transfer._run", return_value=(0, listing, "")):
            self.assertTrue(transfer.key_exists("rclone", "ceph", "crsw",
                                                "rs2/csac/green/2_final/a.csv"))

    def test_missing_key(self):
        with mock.patch("transfer._run",
                        return_value=(1, "", "directory not found")):
            self.assertFalse(transfer.key_exists("rclone", "ceph", "crsw",
                                                 "rs2/csac/green/2_final/nope.csv"))


class TestStatKey(unittest.TestCase):
    def test_returns_entry_with_size(self):
        listing = '[{"Path":"a.csv","Name":"a.csv","Size":1234}]'
        with mock.patch("transfer._run", return_value=(0, listing, "")):
            entry = transfer.stat_key("rclone", "ceph", "crsw", "k/a.csv")
        self.assertEqual(entry["Size"], 1234)

    def test_missing_returns_none(self):
        with mock.patch("transfer._run", return_value=(1, "", "not found")):
            self.assertIsNone(transfer.stat_key("rclone", "ceph", "crsw", "k"))


class TestListProjects(unittest.TestCase):
    def test_lists_and_sorts_dirs(self):
        listing = ('[{"Path":"csac","Name":"csac","IsDir":true},'
                   '{"Path":"aid-flows","Name":"aid-flows","IsDir":true}]')
        with mock.patch("transfer._run", return_value=(0, listing, "")):
            self.assertEqual(transfer.list_projects("rclone", "ceph", "crsw", "rs2"),
                             ["aid-flows", "csac"])

    def test_empty_json_is_empty_list(self):
        with mock.patch("transfer._run", return_value=(0, "[]", "")):
            self.assertEqual(transfer.list_projects("rclone", "ceph", "crsw", "rs2"), [])

    def test_missing_prefix_is_empty_list(self):
        # An empty strand has no prefix in S3 - "directory not found" means
        # genuinely nothing there, not a failure (r4 §2).
        with mock.patch("transfer._run",
                        return_value=(1, "", "error: directory not found")):
            self.assertEqual(transfer.list_projects("rclone", "ceph", "crsw", "rs2"), [])

    def test_permission_failure_is_none(self):
        # A scoped credential may write but not list; None must never be
        # presented as "no existing projects" (r4 §2).
        with mock.patch("transfer._run", return_value=(1, "", "AccessDenied")):
            self.assertIsNone(transfer.list_projects("rclone", "ceph", "crsw", "rs2"))

    def test_network_failure_is_none(self):
        with mock.patch("transfer._run",
                        return_value=(1, "", "dial tcp: i/o timeout")):
            self.assertIsNone(transfer.list_projects("rclone", "ceph", "crsw", "rs2"))

    def test_bad_json_is_none(self):
        with mock.patch("transfer._run", return_value=(0, "not json", "")):
            self.assertIsNone(transfer.list_projects("rclone", "ceph", "crsw", "rs2"))


if __name__ == "__main__":
    unittest.main()
