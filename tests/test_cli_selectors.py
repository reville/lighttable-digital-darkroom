import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from lighttable_cli.selectors import resolve_reference


class AbsolutePhotoReferenceTests(unittest.TestCase):
    def test_absolute_photo_path_reaches_resolver_without_corrupting_query(self):
        client = mock.Mock()
        client.get.return_value = {"name": "3:Photo #1 & café.CR3"}
        path = "/Pictures/Photo #1 & café.CR3"

        self.assertEqual(resolve_reference(client, path), ["3:Photo #1 & café.CR3"])

        client.get.assert_called_once()
        request = urlsplit(client.get.call_args.args[0])
        self.assertEqual(request.path, "/api/resolve")
        self.assertEqual(parse_qs(request.query), {"path": [path]})

    def test_qualified_catalog_reference_does_not_call_path_resolver(self):
        client = mock.Mock()
        self.assertEqual(resolve_reference(client, "3:field.jpg"), ["3:field.jpg"])
        client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
