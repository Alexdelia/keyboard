import pcbnew


def test_pcbnew_importable():
    assert pcbnew.Version().split(".")[0].isdigit()
