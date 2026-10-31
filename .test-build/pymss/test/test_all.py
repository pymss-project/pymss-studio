from __future__ import annotations

import pytest

from pymss import MSSeparator, get_separation_logger

from .cases import SEPARATOR_CASES, SeparatorCase


pytestmark = pytest.mark.integration


def case_id(case: SeparatorCase) -> str:
    return case.name


@pytest.mark.parametrize("case", SEPARATOR_CASES, ids=case_id)
def test_all(case: SeparatorCase) -> None:
    missing_paths = case.missing_paths()
    if missing_paths:
        pytest.skip("missing local test assets: " + ", ".join(str(path) for path in missing_paths))

    logger = get_separation_logger()
    logger.info("running %s", case.name)

    separator = MSSeparator(
        model_type=case.model_type,
        model_path=str(case.model_path),
        config_path=str(case.config_path),
        device=case.device,
        device_ids=list(case.device_ids),
        output_format=case.output_format,
        store_dirs=case.store_dirs,
        logger=logger,
    )

    try:
        processed_files = separator.process_folder(str(case.input_path))
    finally:
        separator.del_cache()

    assert processed_files
