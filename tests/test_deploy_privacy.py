"""The web VM's logs hold no personal data (audit of 9 November 2026).

The access line is nginx's alone: uvicorn's is switched off, because it
carries the browser's address (rewritten from X-Forwarded-For) and the
query string, which is a search's words. nginx's format names no
variable that carries the query, the user agent, the referer or an
address. Stdlib only: these read the deploy files as text."""
import re
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

# nginx variables that would put an identifier or typed content in a line.
FORBIDDEN = ("$request", "$request_uri", "$args", "$arg_", "$query_string",
             "$is_args", "$http_user_agent", "$http_referer", "$remote_addr",
             "$binary_remote_addr", "$proxy_add_x_forwarded_for",
             "$http_x_forwarded_for", "$http_x_real_ip", "$remote_user",
             "$http_x_webauth")


class TestUvicornAccessLog(unittest.TestCase):
    def test_uvicorn_access_log_is_off(self):
        text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        cmd = text[text.index("CMD ["):]
        self.assertIn('"--no-access-log"', cmd)


class TestNginxAccessLog(unittest.TestCase):
    def setUp(self):
        self.text = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        # Comments may name what is left out; only directives count.
        self.code = "\n".join(line.split("#", 1)[0] for line in self.text.splitlines())

    def test_every_access_log_uses_the_crsw_format(self):
        lines = re.findall(r"^\s*access_log\s+([^;]+);", self.code, re.M)
        self.assertTrue(lines, "nginx.conf has no access_log line")
        for args in lines:
            parts = args.split()
            self.assertTrue(parts[0] == "off" or (len(parts) >= 2 and parts[1] == "crsw"),
                            "access_log %r does not use the crsw format" % args)

    def test_the_format_names_nothing_personal(self):
        m = re.search(r"log_format\s+crsw\s+((?:'[^']*'\s*)+);", self.code)
        self.assertIsNotNone(m, "no log_format crsw")
        fmt = m.group(1)
        used = set(re.findall(r"\$[a-z_][a-z0-9_]*", fmt))
        for var in used:
            self.assertFalse(any(var == f or (f.endswith("_") and var.startswith(f))
                                 or (f == "$http_x_webauth" and var.startswith(f))
                                 for f in FORBIDDEN),
                             "log_format crsw uses %s" % var)

    def test_file_names_are_cut_from_the_logged_path(self):
        self.assertIn("map $uri $crsw_log_path", self.code)
        self.assertIn("$crsw_log_path", re.search(r"log_format\s+crsw[^;]+;", self.code).group(0))
        # The block ends at a line holding only "}" (the values hold "${head}").
        block = re.search(r"map \$uri \$crsw_log_path \{(.*?)\n\s*\}\s*\n",
                          self.code, re.S).group(1)
        self.assertIn("/deposits/[^/]+/files/", block)
        self.assertIn("/datasets/.+/files/", block)

    def test_only_one_log_format(self):
        # Another format could reintroduce what this one leaves out.
        self.assertEqual(re.findall(r"^\s*log_format\s+(\S+)", self.code, re.M), ["crsw"])


if __name__ == "__main__":
    unittest.main()
