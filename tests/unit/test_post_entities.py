"""T-026 entity extraction boundaries and normalization."""

from ting_ting.post_entities import (
    extract_hashtags, extract_mentions, linkify_post_content,
)
from ting_ting.search import search_terms


def test_mentions_normalize_dedupe_and_ignore_email_domain():
    assert extract_mentions(
        "Hi @Alice and @alice; mail me at one@alice.com, not @@bob"
    ) == ["alice"]


def test_mentions_follow_username_grammar_and_limit():
    content = " ".join(f"@user_{i}" for i in range(25))
    values = extract_mentions(content)
    assert len(values) == 20
    assert values[0] == "user_0" and values[-1] == "user_19"
    assert extract_mentions("@ab @@bad") == []


def test_hashtags_unicode_nfkc_casefold_and_dedupe():
    assert extract_hashtags("#Python #PYTHON #Việt_Nam ＃fullwidth #１２３") == [
        "python", "việt_nam", "123",
    ]


def test_hashtag_boundaries_and_limit():
    assert extract_hashtags("word#hidden #_bad #good") == ["good"]
    assert len(extract_hashtags(" ".join(f"#tag{i}" for i in range(25)))) == 20


def test_search_terms_strip_operators_bound_length_and_dedupe():
    assert search_terms('  "Hello" OR hello + Việt_Nam ***  ') == [
        "hello", "việt_nam",
    ]
    assert len(search_terms(" ".join(f"term{i}" for i in range(20)))) == 8


def test_linkifier_escapes_all_original_html_and_controls_links():
    rendered = str(linkify_post_content(
        '<img src=x onerror=alert(1)> Hi @Alice #Việt_Nam'
    ))
    assert "<img" not in rendered and "&lt;img" in rendered
    assert 'href="/web/profile/alice"' in rendered
    assert 'href="/web/search?tag=vi%E1%BB%87t_nam"' in rendered
