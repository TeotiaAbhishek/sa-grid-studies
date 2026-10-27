import io
import zipfile

import pytest
import requests
import responses

from sagrid.nemweb.client import (
    BASE_URL,
    DownloadError,
    NemwebClient,
    check_url,
    iter_csv_texts,
    safe_name,
)
from tests.conftest import zip_of

ARCHIVE = BASE_URL + "/Reports/Archive/DispatchIS_Reports/"
CURRENT = BASE_URL + "/Reports/Current/DispatchIS_Reports/"


def listing(*names):
    return "<html>" + "".join(f'<a href="/x/{n}">{n}</a>' for n in names) + "</html>"


@pytest.mark.parametrize(
    "url",
    [
        "http://nemweb.com.au/a.zip",
        "https://evil.com/a.zip",
        "https://nemweb.com.au.evil.com/a.zip",
        "file:///etc/passwd",
    ],
)
def test_rejects_non_allowlisted_urls(url):
    with pytest.raises(DownloadError):
        check_url(url)


@pytest.mark.parametrize("name", ["..\\..\\evil.zip", "a b.zip", "x;rm.zip", "..", ""])
def test_rejects_unsafe_names(name):
    with pytest.raises(DownloadError):
        safe_name(name)


def test_traversal_reduced_to_basename():
    assert safe_name("../../etc/passwd") == "passwd"


def test_safe_name_strips_path():
    assert safe_name("/Reports/PUBLIC_DISPATCHIS_20261001.zip") == "PUBLIC_DISPATCHIS_20261001.zip"


@responses.activate
def test_picks_archive_then_current(tmp_path):
    from datetime import date

    responses.get(
        ARCHIVE, body=listing("PUBLIC_DISPATCHIS_20261001.zip", "PUBLIC_DISPATCHIS_20260930.zip")
    )
    responses.get(
        CURRENT,
        body=listing(
            "PUBLIC_DISPATCHIS_202610020005_0000000001.zip",
            "PUBLIC_DISPATCHIS_202610020010_0000000002.zip",
        ),
    )
    c = NemwebClient(tmp_path, retries=0)
    files = c.files_for_dates("dispatchis", [date(2026, 10, 1), date(2026, 10, 2)])
    names = [n for _, n in files]
    assert names == [
        "PUBLIC_DISPATCHIS_20261001.zip",
        "PUBLIC_DISPATCHIS_202610020005_0000000001.zip",
        "PUBLIC_DISPATCHIS_202610020010_0000000002.zip",
    ]


@responses.activate
def test_missing_day_is_logged(tmp_path, caplog):
    from datetime import date

    responses.get(ARCHIVE, body=listing())
    responses.get(CURRENT, body=listing())
    assert NemwebClient(tmp_path, retries=0).files_for_dates("dispatchis", [date(2026, 1, 1)]) == []
    assert "no file found" in caplog.text


@responses.activate
def test_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    url = ARCHIVE + "f.zip"
    responses.get(url, body=requests.ConnectionError("boom"))
    responses.get(url, status=503)
    responses.get(url, body=b"data")
    f = NemwebClient(tmp_path, retries=3, backoff=0).fetch(url, "f.zip")
    assert f.path.read_bytes() == b"data" and not f.from_cache


@responses.activate
def test_gives_up_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    url = ARCHIVE + "f.zip"
    responses.get(url, status=503)
    with pytest.raises(DownloadError, match="giving up"):
        NemwebClient(tmp_path, retries=2, backoff=0).fetch(url, "f.zip")


@responses.activate
def test_404_is_not_retried(tmp_path):
    url = ARCHIVE + "f.zip"
    responses.get(url, status=404)
    with pytest.raises(DownloadError, match="not found"):
        NemwebClient(tmp_path, retries=3).fetch(url, "f.zip")
    assert len(responses.calls) == 1


@responses.activate
def test_cache_hit_and_corruption_detected(tmp_path):
    url = ARCHIVE + "f.zip"
    responses.get(url, body=b"data")
    c = NemwebClient(tmp_path, retries=0)
    c.fetch(url, "f.zip")
    assert c.fetch(url, "f.zip").from_cache
    (tmp_path / "f.zip").write_bytes(b"corrupted")
    again = c.fetch(url, "f.zip")
    assert not again.from_cache and again.path.read_bytes() == b"data"


def test_iter_nested_zips():
    inner = zip_of({"A.CSV": b"C,x\n"})
    outer = zip_of({"a.zip": inner, "readme.txt": b"skip"})
    assert [n for n, _ in iter_csv_texts(outer)] == ["A.CSV"]


def test_rejects_too_deep_nesting():
    z = zip_of({"x.CSV": b"x"})
    for i in range(4):
        z = zip_of({f"{i}.zip": z})
    with pytest.raises(DownloadError, match="deeper"):
        list(iter_csv_texts(z))


def test_rejects_oversized_member(monkeypatch):
    monkeypatch.setattr("sagrid.nemweb.client.MAX_ZIP_MEMBER_BYTES", 10)
    with pytest.raises(DownloadError, match="too large"):
        list(iter_csv_texts(zip_of({"a.CSV": b"x" * 100})))


def test_bad_zip_raises():
    with pytest.raises(zipfile.BadZipFile):
        list(iter_csv_texts(io.BytesIO(b"nope").getvalue()))
