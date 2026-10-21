"""Tests for strict folder path scheme compilation and rendering."""

import os

import pytest

from shared.scheme import GroupNode, LiteralNode

from shared.paths import (
    DEFAULT_FOLDER_SCHEME,
    MAX_FOLDER_COMPONENT_BYTES,
    REQUIRED_FOLDER_TAG_NAMES,
    STAGED_NAME_LENGTH,
    FolderRenderError,
    FolderScheme,
    FolderSchemeError,
    absolute_path_error,
    absolute_path_limit,
    compile_folder_scheme,
    describe_path_error,
    format_folder_components,
    is_too_long_path,
    normalized_validation_key,
    path_size,
    path_unit_name,
    platform_name,
    render_folder_components,
    sanitize_path_component,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def full_tags():
    """All tags used by the default folder scheme."""
    return {
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
        "block": "Toonami",
        "special": "Holiday",
        "show": "Batman",
    }


@pytest.fixture
def required_tags():
    """Only the three unconditional folder-structure tags."""
    return {
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "2000s",
    }


def render(text, tags):
    """Compile and render a scheme for a concise test assertion."""
    return render_folder_components(compile_folder_scheme(text), tags)


# ---------------------------------------------------------------------------
# Default scheme rendering
# ---------------------------------------------------------------------------

def test_required_folder_tag_set():
    assert REQUIRED_FOLDER_TAG_NAMES == frozenset({
        "network",
        "filler_type",
        "time_period",
    })


def test_default_scheme_full_tags(full_tags):
    scheme = compile_folder_scheme(DEFAULT_FOLDER_SCHEME)

    assert render_folder_components(scheme, full_tags) == (
        "Cartoon Network",
        "Blocks",
        "Toonami",
        "Promo",
        "2000s",
        "Holiday",
    )


def test_default_scheme_omits_optional_fragments(required_tags):
    scheme = compile_folder_scheme(DEFAULT_FOLDER_SCHEME)

    assert render_folder_components(scheme, required_tags) == (
        "Cartoon Network",
        "Promo",
        "2000s",
    )


def test_default_scheme_fallback_uses_show_when_special_missing(required_tags):
    tags = {**required_tags, "show": "Batman"}
    scheme = compile_folder_scheme(DEFAULT_FOLDER_SCHEME)

    assert render_folder_components(scheme, tags)[-1] == "Batman"


def test_canonical_required_tag_names_are_accepted(required_tags):
    scheme = compile_folder_scheme("{network}/{filler_type}/{time_period}")

    assert render_folder_components(scheme, required_tags) == (
        "Cartoon Network",
        "Promo",
        "2000s",
    )


def test_group_can_create_multiple_folders(full_tags):
    scheme = compile_folder_scheme(
        "{network}/[Categories/{block}/Subfolder]/{type}/{time_period}"
    )

    assert render_folder_components(scheme, full_tags) == (
        "Cartoon Network",
        "Categories",
        "Toonami",
        "Subfolder",
        "Promo",
        "2000s",
    )


def test_escaped_portable_literals_are_allowed(required_tags):
    text = r"\{root\}/{network}/{type}/{time_period}"
    scheme = compile_folder_scheme(text)

    assert render_folder_components(scheme, required_tags)[0] == "{root}"


# ---------------------------------------------------------------------------
# Required tag and fallback validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("canonical", sorted(REQUIRED_FOLDER_TAG_NAMES))
def test_each_required_tag_must_be_present(canonical):
    # Build the smallest valid scheme, then remove one required expression.
    placeholders = {
        "network": "{network}",
        "filler_type": "{type}",
        "time_period": "{time_period}",
    }
    del placeholders[canonical]
    text = "/".join(placeholders.values())

    with pytest.raises(FolderSchemeError, match="missing required top-level tags"):
        compile_folder_scheme(text)


@pytest.mark.parametrize(
    "text",
    [
        "[{network}]/{type}/{time_period}",
        "{network}/[{type}]/{time_period}",
        "{network}/{type}/[{time_period}]",
        "{network}/{year,time_period}/{type}",
        "{network,year}/{type}/{time_period}",
    ],
)
def test_required_tags_must_be_exact_top_level_tags(text):
    with pytest.raises(FolderSchemeError, match="exact top-level tag"):
        compile_folder_scheme(text)


def test_fallback_without_required_tags_is_allowed(required_tags):
    scheme = compile_folder_scheme(
        "{network}/{type}/{time_period}/{year,block}"
    )
    tags = {**required_tags, "year": "2000"}

    assert render_folder_components(scheme, tags)[-1] == "2000"


# ---------------------------------------------------------------------------
# Folder grammar and static path validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[]/{network}/{type}/{time_period}", "exactly one tag"),
        ("[{block}{show}]/{network}/{type}/{time_period}", "exactly one tag"),
        ("[[{block}]]/{network}/{type}/{time_period}", "cannot be nested"),
        ("[{block}|{show}]/{network}/{type}/{time_period}", "pipe separators"),
        (r"{network}\/{type}/{time_period}", "cannot escape '/'"),
        (r"{network}\?/{type}/{time_period}", "not valid"),
        (r"\{root\}/{network}/{type}/{time_period}", None),
    ],
)
def test_invalid_group_and_escape_shapes(text, message):
    """Reject nested/OR/unsafe groups while allowing portable escapes."""
    if message is None:
        assert compile_folder_scheme(text).text == text
    else:
        with pytest.raises(FolderSchemeError, match=message):
            compile_folder_scheme(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        (f" {{DEFAULT}}/{DEFAULT_FOLDER_SCHEME}", "surrounding whitespace"),
        ("{network}\n/{type}/{time_period}", "single line"),
        ("{network/{type}/{time_period}", "syntax"),
        ("{network}/{type}/{time_period}}", "syntax"),
        ("{network}\u0085/{type}/{time_period}", "single line"),
        ("{network}\u2028/{type}/{time_period}", "single line"),
        ("{network}\u2029/{type}/{time_period}", "single line"),
        ("{network}/{color}/{time_period}", "Unknown folder tag"),
        ("{network}/{}/{time_period}", "syntax"),
        ("/{network}/{type}/{time_period}", "absolute path"),
        ("{network}//{type}/{time_period}", "empty path component"),
        ("C:/{network}/{type}/{time_period}", "drive-qualified"),
        ("../{network}/{type}/{time_period}", "unsupported component"),
        ("CON/{network}/{type}/{time_period}", "reserved device name"),
        ("COM¹/{network}/{type}/{time_period}", "reserved device name"),
        ("Bad?Name/{network}/{type}/{time_period}", "portable-invalid"),
        ("Bad\u202eName/{network}/{type}/{time_period}", "portable-invalid"),
        (
            "[Bad?Name/{block}/]/{network}/{type}/{time_period}",
            "portable-invalid",
        ),
    ],
)
def test_invalid_schemes_are_rejected(text, message):
    """Reject malformed, rooted, unsafe, and non-portable scheme text."""
    with pytest.raises(FolderSchemeError, match=message):
        compile_folder_scheme(text)


def test_static_component_length_is_validated():
    literal = "A" * (MAX_FOLDER_COMPONENT_BYTES + 1)
    text = f"{literal}/{{network}}/{{type}}/{{time_period}}"

    with pytest.raises(FolderSchemeError, match="exceeding 255 UTF-8 bytes"):
        compile_folder_scheme(text)


def test_invalid_unicode_is_a_typed_error(required_tags):
    static_scheme = "{network}/\ud800/{type}/{time_period}"
    with pytest.raises(FolderSchemeError, match="invalid Unicode"):
        compile_folder_scheme(static_scheme)

    scheme = compile_folder_scheme("{network}/{type}/{time_period}")
    tags = {**required_tags, "network": "\ud800"}
    with pytest.raises(FolderRenderError, match="invalid Unicode"):
        render_folder_components(scheme, tags)


# ---------------------------------------------------------------------------
# Runtime values and optional-group omission
# ---------------------------------------------------------------------------

def test_missing_required_value_fails_at_render(required_tags):
    scheme = compile_folder_scheme(DEFAULT_FOLDER_SCHEME)
    tags = {**required_tags, "network": ""}

    with pytest.raises(FolderRenderError, match=r"Tag expression \{network\}"):
        render_folder_components(scheme, tags)


def test_missing_optional_group_is_allowed(required_tags):
    scheme = compile_folder_scheme("{network}/[{block}/]{type}/{time_period}")

    assert render_folder_components(scheme, required_tags) == (
        "Cartoon Network",
        "Promo",
        "2000s",
    )


@pytest.mark.parametrize(
    "text",
    [
        "{network}/[{block}]/{type}/{time_period}",
        "[{block}]/{network}/{type}/{time_period}",
        "{network}/{type}/{time_period}/[{block}]/",
    ],
)
def test_omitted_group_separators_are_collapsed(text, required_tags):
    """External separators disappear cleanly when their group is omitted."""
    scheme = compile_folder_scheme(text)

    assert render_folder_components(scheme, required_tags) == (
        "Cartoon Network",
        "Promo",
        "2000s",
    )


def test_nonempty_value_sanitized_to_empty_fails_inside_group(required_tags):
    scheme = compile_folder_scheme(
        "{network}/[{block}/]{type}/{time_period}"
    )
    tags = {**required_tags, "block": "..."}

    with pytest.raises(FolderRenderError, match="empty after sanitization"):
        render_folder_components(scheme, tags)


def test_nonempty_fallback_value_sanitized_to_empty_does_not_fall_through(required_tags):
    scheme = compile_folder_scheme(
        "{network}/{type}/{time_period}/[{special,show}/]"
    )
    tags = {**required_tags, "special": "   ", "show": "Batman"}

    with pytest.raises(FolderRenderError, match="Tag 'special'"):
        render_folder_components(scheme, tags)


# ---------------------------------------------------------------------------
# Sanitization, portability, and size limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("../../Outside", "..-..-Outside"),
        ("/absolute/path", "-absolute-path"),
        ("C:\\Temp", "C-Temp"),
        ("\\\\server\\share", "-server-share"),
        ("A///B", "A-B"),
        ("  Café   /  World  ", "Café - World"),
        ("NUL.txt", "_NUL.txt"),
        ("COM¹", "_COM¹"),
        ("LPT².txt", "_LPT².txt"),
        ("A\x7f\x80B", "A-B"),
        ("A\u200b\u202e\u2066B", "A-B"),
        ("Example.  ", "Example"),
    ],
)
def test_tag_sanitization_cannot_inject_path_structure(value, expected, required_tags):
    """Tag values become one safe component regardless of path-like input."""
    scheme = compile_folder_scheme("{network}/{type}/{time_period}")
    tags = {**required_tags, "network": value}

    assert render_folder_components(scheme, tags)[0] == expected


def test_sanitizer_preserves_case_and_normalizes_validation_keys():
    sanitized = sanitize_path_component("Café  TV")

    assert sanitized == "Café TV"
    assert normalized_validation_key(sanitized) == normalized_validation_key("CAFÉ tv")
    assert sanitize_path_component(sanitized) == sanitized


def test_sanitizer_rejects_empty_and_oversized_components():
    with pytest.raises(ValueError, match="empty after sanitization"):
        sanitize_path_component(" .. ")

    with pytest.raises(ValueError, match="exceeds 255 UTF-8 bytes"):
        sanitize_path_component("A" * (MAX_FOLDER_COMPONENT_BYTES + 1))


def test_a_rendered_relative_path_is_not_limited_by_its_own_length(required_tags):
    """The relative path has no rule of its own any more.

    A relative path is always shorter than the absolute path that contains it,
    so a limit on it could never bind once the platform's limit on the whole
    path exists.  What is left is the per-component rule, which is `NAME_MAX`
    and is checked where the component is built.
    """
    repeated_year = "/".join(["{year}"] * 17)
    text = f"{repeated_year}/{{network}}/{{type}}/{{time_period}}"
    scheme = compile_folder_scheme(text)
    tags = {
        **required_tags,
        "year": "Y" * MAX_FOLDER_COMPONENT_BYTES,
    }

    components = render_folder_components(scheme, tags)

    assert all(len(component.encode("utf-8")) <= MAX_FOLDER_COMPONENT_BYTES
               for component in components)


# ---------------------------------------------------------------------------
# Compiled-scheme integrity and display formatting
# ---------------------------------------------------------------------------

def test_components_are_display_cased_safe_values(full_tags):
    components = render_folder_components(
        compile_folder_scheme(DEFAULT_FOLDER_SCHEME),
        full_tags,
    )

    assert components == tuple(component for component in components if component)
    assert all("/" not in component and "\\" not in component for component in components)


def test_format_folder_components_adds_trailing_separator():
    assert format_folder_components(("Cartoon Network", "Promo")) == (
        "Cartoon Network/Promo/"
    )
    assert format_folder_components(()) == ""


def test_renderer_rejects_uncompiled_scheme(required_tags):
    with pytest.raises(TypeError, match="compiled FolderScheme"):
        render_folder_components(DEFAULT_FOLDER_SCHEME, required_tags)


def test_compiled_scheme_is_reusable(full_tags):
    scheme = compile_folder_scheme(DEFAULT_FOLDER_SCHEME)

    assert isinstance(scheme, FolderScheme)
    assert render_folder_components(scheme, full_tags) == render_folder_components(
        scheme,
        full_tags,
    )


def test_render_uses_source_when_compiled_nodes_are_mutated(required_tags):
    """The compiler's source text wins if public AST nodes are modified."""
    scheme = compile_folder_scheme(
        "{network}/[{block}]/{type}/{time_period}"
    )
    group = next(node for node in scheme.nodes if isinstance(node, GroupNode))
    # The outer compiled object is frozen, but shared AST lists remain mutable.
    group.elements.insert(0, LiteralNode("/"))
    tags = {**required_tags, "block": "Toonami"}

    assert render_folder_components(scheme, tags) == (
        "Cartoon Network",
        "Toonami",
        "Promo",
        "2000s",
    )


# ---------------------------------------------------------------------------
# The absolute path length
# ---------------------------------------------------------------------------

def _root_of_length(length):
    """An absolute root path of exactly `length` characters."""
    prefix = os.path.abspath(os.sep)
    return prefix + "A" * (length - len(prefix))


def test_the_limit_is_the_platforms_own():
    """Windows' 260 is UTF-16 code units, macOS' 1024 and Linux' 4096 are bytes.

    These are the filesystem's limits, not commcut's, and they are the numbers
    the refusal quotes.
    """
    import sys

    expected = {"win32": 260, "darwin": 1024, "linux": 4096}
    key = {"win32": "windows", "darwin": "darwin", "linux": "linux"}[
        sys.platform
    ]

    assert absolute_path_limit() == expected[sys.platform]
    assert path_unit_name() == ("characters" if key == "windows" else "bytes")


def test_a_destination_that_exactly_fits_is_accepted():
    limit = absolute_path_limit()
    # A short name, so the staged temporary file is the longest one written
    # beside it, and the folder is sized to leave exactly room for it.
    root = _root_of_length(limit - 1 - STAGED_NAME_LENGTH)

    assert absolute_path_error(root, ["Clip.mp4"], ["Clip.cnfo"]) is None


def test_a_destination_one_character_over_is_refused():
    limit = absolute_path_limit()
    root = _root_of_length(limit - STAGED_NAME_LENGTH)

    error = absolute_path_error(root, ["Clip.mp4"], ["Clip.cnfo"])

    assert error is not None
    assert "too long" in error


def test_the_refusal_names_the_path_the_length_the_limit_and_the_split():
    limit = absolute_path_limit()
    root = _root_of_length(limit - STAGED_NAME_LENGTH + 1)

    error = absolute_path_error(
        root, ["Clip.mp4"], ["Clip.cnfo"], label="Segment 3")

    assert error is not None
    assert error.startswith("Segment 3 cannot be written")
    assert os.path.join(root, "Clip.mp4") in error
    assert str(limit) in error
    assert "characters" in error or "bytes" in error
    # The three parts the person has to choose between.
    assert f"{len(root)} " in error
    assert "the file name" in error
    assert "Shorten the tag values" in error


def test_the_refusal_accounts_for_the_staged_temporary_file():
    """A short clip name in a deep folder fails on its own temporary file.

    The staged name is longer than the clip's, so the folder is measured
    against the staged name and not against the name the user sees.
    """
    limit = absolute_path_limit()
    root = _root_of_length(limit - 1 - STAGED_NAME_LENGTH + 1)

    error = absolute_path_error(root, ["a.mp4"], ["a.cnfo"])

    assert error is not None
    assert "staged temporary file" in error


def test_a_record_one_character_longer_than_the_clip_is_measured_separately():
    limit = absolute_path_limit()
    # Names long enough that the record, not the staged file, is the longest
    # one written beside the clip: the record is one character longer, and it
    # is measured on its own rather than assumed from the clip.
    clip_name = "C" * 40 + ".mp4"
    record_name = "C" * 40 + ".cnfo"
    root = _root_of_length(limit - 1 - len(record_name))

    assert absolute_path_error(root, [clip_name]) is None
    assert absolute_path_error(root, [clip_name], [record_name]) is None
    assert absolute_path_error(
        root, [clip_name], ["C" * 41 + ".cnfo"]) is not None


def test_path_size_counts_a_character_outside_the_bmp_as_two_units():
    """Windows counts UTF-16 code units, and `len()` would not see it."""
    import sys

    if sys.platform != "win32":
        pytest.skip("the UTF-16 unit is a Windows rule")

    assert path_size("a") == 1
    assert path_size("\U0001F600") == 2
    assert path_size("café") == 4


def test_the_staged_name_budget_covers_every_writer(tmp_path):
    """The constant cannot drift: measure the names the app really stages.

    Both writers stage in the *destination* directory, so a staged path can be
    longer than the path it becomes, and the budget is what keeps a plan that
    passes from failing at the write.
    """
    import tempfile

    from shared import ffmpeg, records

    for prefix, suffix in (
        (ffmpeg._TEMP_PREFIX, ffmpeg._TEMP_SUFFIX),
        (records._TEMP_PREFIX, records._TEMP_SUFFIX),
    ):
        descriptor, path = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=str(tmp_path))
        os.close(descriptor)
        os.remove(path)

        assert len(os.path.basename(path)) <= STAGED_NAME_LENGTH


def test_a_missing_file_is_not_reported_as_a_path_that_is_too_long():
    """Windows says "path not found" for an over-long path, and the length is
    what tells the two apart."""
    error = OSError(3, "The system cannot find the path specified")
    error.filename = "C:\\short\\name.mp4"
    error.winerror = 3

    assert not is_too_long_path(error)
    assert describe_path_error(error) == (
        "The system cannot find the path specified")


def test_a_path_that_is_too_long_is_reported_as_one():
    limit = absolute_path_limit()
    long_path = "C:\\" + "A" * limit

    error = OSError(3, "The system cannot find the path specified")
    error.filename = long_path
    error.winerror = 3

    assert is_too_long_path(error)
    message = describe_path_error(error)
    assert "too long" in message
    assert long_path in message
    assert platform_name() in message


def test_an_error_with_no_path_is_left_alone():
    error = OSError(13, "Permission denied")

    assert not is_too_long_path(error)
    assert describe_path_error(error) == "Permission denied"
