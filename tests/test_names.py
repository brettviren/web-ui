import pytest

from web_ui.names import (
    NameError_,
    Target,
    parse_host,
    slugify,
    split_host_header,
    url_for,
)


@pytest.mark.parametrize(
    "header,target",
    [
        ("localhost:8484", Target(None)),
        ("127.0.0.1:8484", Target(None)),
        ("[::1]:8484", Target(None)),
        ("hosta.localhost:8484", Target("hosta")),
        ("maggie.hosta.localhost", Target("hosta", "maggie")),
        ("ProjX.Maggie.HostA.localhost:8484", Target("hosta", "maggie", "projx")),
        ("projx.maggie.hosta.localhost.", Target("hosta", "maggie", "projx")),
    ],
)
def test_parse_host(header, target):
    assert parse_host(header) == target


@pytest.mark.parametrize(
    "header",
    ["evil.example.com", "evil.example.com:8484", "a.b.c.d.localhost", "x_y.localhost", "-x.localhost", "localhost:abc", "[::1"],
)
def test_parse_host_rejects(header):
    with pytest.raises(NameError_):
        parse_host(header)


def test_split():
    assert split_host_header("A.localhost:80") == ("a.localhost", 80)
    assert split_host_header("[::1]") == ("[::1]", None)


def test_url_for():
    assert url_for("hosta", "maggie", "projx", port=8484) == "http://projx.maggie.hosta.localhost:8484/"
    assert url_for("hosta", "agentsview", "default", port=8484) == "http://agentsview.hosta.localhost:8484/"
    assert url_for("hosta") == "http://hosta.localhost/"


def test_slugify():
    assert slugify("My Project_2") == "my-project-2"
    assert slugify("...") == "x"
    assert len(slugify("a" * 100)) == 40
