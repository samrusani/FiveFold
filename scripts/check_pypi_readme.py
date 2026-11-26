"""Reject relative URLs in the rendered description of a wheel or sdist."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class DescriptionLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.relative: list[tuple[str, str]] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = {"a": "href", "img": "src"}.get(tag)
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)
            if name == attribute and value is not None and not urlsplit(value).scheme:
                self.relative.append((tag, value))


def read_metadata(path: Path) -> bytes:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            (name,) = (name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
            return wheel.read(name)
    with tarfile.open(path) as sdist:
        (member,) = (
            member
            for member in sdist.getmembers()
            if len(PurePosixPath(member.name).parts) == 2
            and PurePosixPath(member.name).name == "PKG-INFO"
        )
        metadata = sdist.extractfile(member)
        if metadata is None:
            raise ValueError(f"{path.name}: PKG-INFO is not a file")
        with metadata:
            return metadata.read()


def main(paths: list[str]) -> int:
    if not paths:
        print("Provide a wheel or source distribution to check.", file=sys.stderr)
        return 1
    from readme_renderer.markdown import render

    failed = False
    for name in paths:
        path = Path(name)
        metadata = BytesParser().parsebytes(read_metadata(path))
        markdown = metadata.get_payload()
        if metadata.get("Description-Content-Type") != "text/markdown" or not isinstance(
            markdown, str
        ):
            raise ValueError(f"{path.name}: expected a Markdown description")
        html = render(markdown, stream=sys.stderr)
        if not html:
            raise ValueError(f"{path.name}: the description did not render")
        links = DescriptionLinks()
        links.feed(html)
        links.close()
        # The renderer adds links to headings in this same description.
        relative = [
            url
            for tag, url in links.relative
            if not (tag == "a" and url.startswith("#") and url[1:] in links.ids)
        ]
        for url in relative:
            print(f"{path.name}: relative URL {url!r}", file=sys.stderr)
        failed |= bool(relative)
    return int(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
