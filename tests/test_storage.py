from pathlib import Path

import pytest

from twod_fim_jobs.exceptions import WriteFailureError
from twod_fim_jobs.utils.storage import copy_file

_FILE_CONTENT = b"test output from twod-fim-jobs"


@pytest.fixture
def src_file(tmp_path: Path) -> Path:
    src = tmp_path / "src.bin"
    src.write_bytes(_FILE_CONTENT)
    return src


def test_copy_file_local(src_file: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst.bin"

    copy_file(str(src_file), str(dst))

    assert dst.read_bytes() == _FILE_CONTENT


def test_copy_file_raises_write_failure_error_on_io_error(tmp_path: Path) -> None:
    src = "/tmp/\x00invalid.bin"  # null byte makes this an illegal path on all POSIX systems
    dst = tmp_path / "dst.bin"

    with pytest.raises(WriteFailureError):
        copy_file(src, str(dst))
