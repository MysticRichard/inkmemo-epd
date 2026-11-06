from inkmemo.sources.wps import _relative_index


def test_wps_merge_coordinates_are_zero_based():
    assert _relative_index(1, 0) == 1
    assert _relative_index(6, 0) == 6
    assert _relative_index(4, 4) == 0
    assert _relative_index(7, 4) == 3
