"""C4.2 numeric-IP proof: obfuscated loopback forms fail closed hermetically."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from meco_news import urls
from meco_news.urls import URLPolicyError, validate_resolved_addresses

# Decimal, hexadecimal, and octal spellings of 127.0.0.1.  The syntax layer
# (validate_url) only recognizes dotted/colon literals, so these hostnames
# reach the resolver; on glibc-style resolvers getaddrinfo maps them to
# 127.0.0.1 (inet_aton legacy).  The resolution layer must reject the answer
# regardless of the textual form, and the classifier must fail closed on
# anything it cannot parse.
NUMERIC_LOOPBACK_FORMS = (
    "2130706433",
    "0x7f000001",
    "0177.0.0.1",
    "0x7f.0.0.1",
    "0177.00.00.01",
    "127.1",
)


def _loopback_answer(hostname: str, port: int, *args: object, **kwargs: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    del args, kwargs, port
    if hostname in NUMERIC_LOOPBACK_FORMS:
        return [(0, 0, 0, "", ("127.0.0.1", 443))]
    return [(0, 0, 0, "", ("93.184.216.34", 443))]


class NumericIpFormTests(unittest.TestCase):
    def test_classifier_fails_closed_on_obfuscated_forms(self) -> None:
        for form in (*NUMERIC_LOOPBACK_FORMS, "::ffff:127.0.0.1", "10.0.0.1"):
            self.assertTrue(urls._is_forbidden_address(form), form)
        self.assertFalse(urls._is_forbidden_address("93.184.216.34"))
        self.assertFalse(urls._is_forbidden_address("2606:2800:220:1:248:1893:25c8:1946"))

    def test_resolution_layer_rejects_resolved_loopback_for_every_form(self) -> None:
        with patch.object(urls.socket, "getaddrinfo", side_effect=_loopback_answer):
            for form in NUMERIC_LOOPBACK_FORMS:
                with self.subTest(hostname=form):
                    with self.assertRaises(URLPolicyError) as error:
                        validate_resolved_addresses(form, 443)
                    self.assertEqual(error.exception.reason_code, "ssrf_address_class")
            self.assertEqual(validate_resolved_addresses("example.com", 443), ["93.184.216.34"])


if __name__ == "__main__":
    unittest.main()
