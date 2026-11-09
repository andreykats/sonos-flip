#!/usr/bin/env python3
"""Release notes from CHANGELOG.md, used by the release workflow.

  scripts/release_notes.py notes X.Y.Z   the X.Y.Z section, for its GitHub release
  scripts/release_notes.py overview      README.md followed by the released changelog, for Docker Hub

Sections are "## X.Y.Z - YYYY-MM-DD" (or "## Unreleased") headings in CHANGELOG.md.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEADING = re.compile(r"^## (?P<title>\S+)(?: - (?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.M)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class ChangelogError(Exception):
    pass


def sections(changelog):
    """[(title, date or None, body)] in file order. Raises ChangelogError on a malformed heading."""
    found = list(HEADING.finditer(changelog))
    stray = [line for line in changelog.splitlines()
             if line.startswith("## ") and not HEADING.match(line)]
    if stray:
        raise ChangelogError(f"malformed heading {stray[0]!r}; expected '## X.Y.Z - YYYY-MM-DD' or '## Unreleased'")
    out = []
    for i, m in enumerate(found):
        title = m["title"]
        if title != "Unreleased" and not (VERSION.match(title) and m["date"]):
            raise ChangelogError(f"malformed heading {m.group(0)!r}; expected '## X.Y.Z - YYYY-MM-DD' or '## Unreleased'")
        end = found[i + 1].start() if i + 1 < len(found) else len(changelog)
        out.append((title, m["date"], changelog[m.end():end].strip()))
    return out


def notes(changelog, version):
    """The body of version's section. Raises ChangelogError if it's missing or empty."""
    for title, _, body in sections(changelog):
        if title == version:
            if not body:
                raise ChangelogError(f"the {version} section of CHANGELOG.md is empty")
            return body + "\n"
    raise ChangelogError(f"CHANGELOG.md has no section for {version}")


def overview(readme, changelog):
    """README followed by every released section, one heading level down (Unreleased left out)."""
    parts = [readme.rstrip(), "## Changelog"]
    for title, date, body in sections(changelog):
        if title == "Unreleased":
            continue
        parts.append(f"### {title} - {date}")
        if body:
            parts.append(re.sub(r"^(#+) ", r"#\1 ", body, flags=re.M))
    return "\n\n".join(parts) + "\n"


def main(argv):
    try:
        changelog = (ROOT / "CHANGELOG.md").read_text()
        if len(argv) == 2 and argv[0] == "notes" and VERSION.match(argv[1]):
            sys.stdout.write(notes(changelog, argv[1]))
        elif len(argv) == 1 and argv[0] == "overview":
            sys.stdout.write(overview((ROOT / "README.md").read_text(), changelog))
        else:
            sys.stderr.write(__doc__)
            return 2
    except (OSError, ChangelogError) as e:
        sys.stderr.write(f"release_notes: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
