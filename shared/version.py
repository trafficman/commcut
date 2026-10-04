"""The version of commcut, in one place.

Applies to: `packaging/build.py`, `.github/workflows/release.yml`.

There is exactly one release, so the number lives in the code rather than in
the build tooling: a tag that disagrees with ``VERSION`` is refused by
``packaging/build.py`` instead of quietly becoming the version. A mistyped tag
is otherwise indistinguishable from a good one until somebody reads the release
page.

The constant is bare -- no ``v`` prefix, no build metadata, no pre-release
suffix -- and a tag is valid when it is ``v`` followed by it. The prefix is a
tag convention, not part of the version, so it is stripped rather than stored.
"""

VERSION = "0.2.7-alpha"
