import unittest

from crsw_deposit import labels


class TestObjectLabels(unittest.TestCase):
    def test_four_labels_in_fixed_order(self):
        out = labels.object_labels("u", "c" * 64, "green", "k1078591")
        self.assertEqual(list(out.keys()), list(labels.LABEL_NAMES))
        self.assertEqual(out, {
            "dataset-uuid": "u",
            "checksum-sha256": "c" * 64,
            "sensitivity": "green",
            "depositor": "k1078591",
        })

    def test_names_are_the_r5_set(self):
        # r5 Q2: the minimal travelling thread. Renaming any of these
        # silently breaks every consumer reading labels off the cluster.
        self.assertEqual(labels.LABEL_NAMES, (
            "dataset-uuid", "checksum-sha256", "sensitivity", "depositor"))

    def test_s3_header_form_adds_prefix_to_every_label(self):
        out = labels.as_s3_headers(
            labels.object_labels("u", "c", "amber", "d"))
        self.assertEqual(out, {
            "x-amz-meta-dataset-uuid": "u",
            "x-amz-meta-checksum-sha256": "c",
            "x-amz-meta-sensitivity": "amber",
            "x-amz-meta-depositor": "d",
        })


if __name__ == "__main__":
    unittest.main()
